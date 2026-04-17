"""Authentication routes: signup/login via phone OTP, email OTP fallback, logout."""
import logging
import os
import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import auth
import email_service
import sms

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Cookie defaults — HTTPS-only in prod, relaxed in dev
_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "true").lower() != "false"


def _mask_phone(e164: str) -> str:
    """Mask all but last 4 digits for UX: +15551234567 → +1 (•••) •••-4567."""
    if len(e164) < 5:
        return "•••"
    return f"{e164[:2]} (•••) •••-{e164[-4:]}"


def _mask_email(email: str) -> str:
    if "@" not in email:
        return "•••"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = "•" * len(local)
    else:
        masked_local = local[0] + "•••" + local[-1]
    return f"{masked_local}@{domain}"


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=auth.SESSION_COOKIE,
        value=token,
        max_age=auth.SESSION_TTL_DAYS * 86400,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response) -> None:
    response.delete_cookie(key=auth.SESSION_COOKIE, path="/")


# ----- Page renderers (unauthenticated) -------------------------------------

@router.get("/login")
async def login_page(request: Request):
    user = await auth.current_user(request)
    if user:
        return RedirectResponse("/app", status_code=303)
    return templates.TemplateResponse(request, "auth/login.html", {"mode": "login"})


@router.get("/signup")
async def signup_page(request: Request):
    user = await auth.current_user(request)
    if user:
        return RedirectResponse("/app", status_code=303)
    return templates.TemplateResponse(request, "auth/login.html", {"mode": "signup"})


# ----- API: send/verify SMS OTP ---------------------------------------------

@router.post("/auth/send-otp")
async def send_otp(request: Request, phone: str = Form(...)):
    phone_e164 = auth.normalize_phone_us(phone)
    ip = request.client.host if request.client else "unknown"
    # Rate-limit per phone and per IP
    await auth.check_and_record_rate(phone_e164, "send", auth.OTP_SEND_RATE)
    await auth.check_and_record_rate(f"ip:{ip}", "send", auth.OTP_SEND_RATE)
    await sms.send_sms_otp(phone_e164)
    existing = await auth.get_user_by_phone(phone_e164)
    return {
        "ok": True,
        "phone_masked": _mask_phone(phone_e164),
        "is_new_user": existing is None,
        "email_fallback_available": bool(existing and existing.get("email")),
    }


@router.post("/auth/verify-otp")
async def verify_otp(request: Request, phone: str = Form(...), code: str = Form(...)):
    phone_e164 = auth.normalize_phone_us(phone)
    ip = request.client.host if request.client else "unknown"
    await auth.check_and_record_rate(phone_e164, "verify", auth.OTP_VERIFY_RATE)
    await auth.check_and_record_rate(f"ip:{ip}", "verify", auth.OTP_VERIFY_RATE)

    code = re.sub(r"\D", "", code or "")
    if len(code) != 6:
        raise HTTPException(400, "Enter the 6-digit code")

    if not await sms.verify_sms_otp(phone_e164, code):
        raise HTTPException(400, "Incorrect or expired code")

    user = await auth.get_user_by_phone(phone_e164)
    if not user:
        user = await auth.create_user(phone_e164)
    await auth.mark_login(user["id"])

    token = await auth.create_session(
        user["id"], ip=ip, ua=request.headers.get("user-agent", "")[:500]
    )
    needs_profile = not user.get("profile_completed_at")
    response = JSONResponse(
        {"ok": True, "next": "/signup/profile" if needs_profile else "/app"}
    )
    _set_session_cookie(response, token)
    return response


# ----- API: email OTP fallback (existing users only) ------------------------

@router.post("/auth/send-email-otp")
async def send_email_otp_route(request: Request, phone: str = Form(...)):
    phone_e164 = auth.normalize_phone_us(phone)
    ip = request.client.host if request.client else "unknown"
    await auth.check_and_record_rate(phone_e164, "send", auth.OTP_SEND_RATE)
    await auth.check_and_record_rate(f"ip:{ip}", "send", auth.OTP_SEND_RATE)

    user = await auth.get_user_by_phone(phone_e164)
    if not user or not user.get("email"):
        # Don't reveal whether the phone is registered
        return {"ok": True, "email_masked": None}

    code = await auth.create_email_otp(phone_e164, user["email"])
    await email_service.send_email_otp(user["email"], code, _mask_phone(phone_e164))
    return {"ok": True, "email_masked": _mask_email(user["email"])}


@router.post("/auth/verify-email-otp")
async def verify_email_otp_route(request: Request, phone: str = Form(...), code: str = Form(...)):
    phone_e164 = auth.normalize_phone_us(phone)
    ip = request.client.host if request.client else "unknown"
    await auth.check_and_record_rate(phone_e164, "verify", auth.OTP_VERIFY_RATE)
    await auth.check_and_record_rate(f"ip:{ip}", "verify", auth.OTP_VERIFY_RATE)

    code = re.sub(r"\D", "", code or "")
    if len(code) != 6:
        raise HTTPException(400, "Enter the 6-digit code")

    if not await auth.verify_email_otp(phone_e164, code):
        raise HTTPException(400, "Incorrect or expired code")

    user = await auth.get_user_by_phone(phone_e164)
    if not user:
        raise HTTPException(400, "User not found")
    await auth.mark_login(user["id"])

    token = await auth.create_session(
        user["id"], ip=ip, ua=request.headers.get("user-agent", "")[:500]
    )
    response = JSONResponse({"ok": True, "next": "/app"})
    _set_session_cookie(response, token)
    return response


# ----- Profile completion (new users after first OTP) -----------------------

@router.get("/signup/profile")
async def profile_page(request: Request):
    user = await auth.current_user(request)
    if not user:
        return RedirectResponse("/signup", status_code=303)
    if user.get("profile_completed_at"):
        return RedirectResponse("/app", status_code=303)
    return templates.TemplateResponse(request, "auth/profile.html", {"user": user})


@router.post("/auth/complete-profile")
async def complete_profile(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    agree: str = Form(default=""),
):
    user = await auth.current_user(request)
    if not user:
        raise HTTPException(401, "Not signed in")
    full_name = full_name.strip()
    email = email.strip().lower()
    if not full_name or len(full_name) > 120:
        raise HTTPException(400, "Please enter your full name")
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(400, "Please enter a valid email address")
    if agree != "on":
        raise HTTPException(400, "You must agree to the Terms and Privacy Policy to continue")
    await auth.update_user_profile(user["user_id"], full_name, email)
    return {"ok": True, "next": "/app"}


# ----- Profile update (from settings page) ----------------------------------

@router.patch("/auth/profile")
async def update_profile(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
):
    user = await auth.current_user(request)
    if not user:
        raise HTTPException(401, "Not signed in")
    full_name = full_name.strip()
    email = email.strip().lower()
    if not full_name or len(full_name) > 120:
        raise HTTPException(400, "Please enter your full name")
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(400, "Please enter a valid email address")
    await auth.update_user_profile(user["user_id"], full_name, email)
    return {"ok": True}


# ----- Logout ---------------------------------------------------------------

@router.post("/auth/logout")
async def logout(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        await auth.delete_session(token)
    response = RedirectResponse("/", status_code=303)
    _clear_session_cookie(response)
    return response
