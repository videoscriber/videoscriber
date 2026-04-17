"""Session management, user lookup, and OTP helpers."""
import hashlib
import hmac
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite
import phonenumbers
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from database import DB_PATH

SESSION_COOKIE = "vs_session"
SESSION_TTL_DAYS = 30
EMAIL_OTP_TTL_MINUTES = 10
EMAIL_OTP_MAX_ATTEMPTS = 5

# Rate limits: max N actions per window (seconds) keyed on phone/IP
OTP_SEND_RATE = (3, 300)      # 3 sends per 5 min per phone/IP
OTP_VERIFY_RATE = (10, 300)   # 10 verifies per 5 min

TOS_VERSION = "2026-04-16"
PRIVACY_VERSION = "2026-04-16"


def normalize_phone_us(raw: str) -> str:
    """Parse a US phone number and return E.164 format. Raises HTTPException on invalid."""
    if not raw:
        raise HTTPException(400, "Phone number is required")
    try:
        parsed = phonenumbers.parse(raw, "US")
    except phonenumbers.NumberParseException:
        raise HTTPException(400, "Invalid phone number")
    if not phonenumbers.is_valid_number(parsed):
        raise HTTPException(400, "Invalid phone number")
    if parsed.country_code != 1:
        raise HTTPException(400, "Only US phone numbers are supported at this time")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def hash_code(code: str) -> str:
    """Deterministic hash of an OTP code for storage. Keyed by a server secret."""
    key = os.getenv("OTP_HASH_KEY", "dev-not-secret").encode()
    return hmac.new(key, code.encode(), hashlib.sha256).hexdigest()


def generate_otp_code() -> str:
    """6-digit numeric OTP."""
    return f"{secrets.randbelow(1_000_000):06d}"


# ----- Rate limiting ---------------------------------------------------------

async def _cleanup_rate_limits(db: aiosqlite.Connection) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await db.execute("DELETE FROM otp_rate_limits WHERE created_at < ?", (cutoff,))


async def check_and_record_rate(key: str, action: str, limit: tuple[int, int]) -> None:
    """Raise 429 if `key` has exceeded `limit[0]` actions in the last `limit[1]` seconds."""
    max_count, window_sec = limit
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_sec)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await _cleanup_rate_limits(db)
        async with db.execute(
            "SELECT COUNT(*) FROM otp_rate_limits WHERE key = ? AND action = ? AND created_at >= ?",
            (key, action, cutoff),
        ) as cur:
            (count,) = await cur.fetchone()
        if count >= max_count:
            raise HTTPException(429, "Too many attempts. Please wait a few minutes and try again.")
        await db.execute(
            "INSERT INTO otp_rate_limits (key, action, created_at) VALUES (?, ?, ?)",
            (key, action, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


# ----- Users -----------------------------------------------------------------

async def get_user_by_phone(phone: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE phone = ?", (phone,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user_by_email(email: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE email = ?", (email,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_user_email_only(email: str) -> dict:
    """Create a user identified by email (phone optional, added later)."""
    user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (id, email, created_at) VALUES (?, ?, ?)",
            (user_id, email, now),
        )
        await db.commit()
    return {"id": user_id, "email": email, "created_at": now}


async def get_user(user_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def ensure_desktop_user() -> dict:
    """Seed the synthetic single-user account used by desktop mode."""
    existing = await get_user(DESKTOP_USER_ID)
    if existing:
        return existing
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (id, full_name, profile_completed_at, plan, created_at) "
            "VALUES (?, ?, ?, 'plus', ?)",
            (DESKTOP_USER_ID, "You", now, now),
        )
        await db.commit()
    return await get_user(DESKTOP_USER_ID)


async def create_user(phone: str) -> dict:
    user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (id, phone, created_at) VALUES (?, ?, ?)",
            (user_id, phone, now),
        )
        await db.commit()
    return {"id": user_id, "phone": phone, "created_at": now}


async def update_user_profile(user_id: str, full_name: str, email: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    sig = default_signature_for(full_name)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET full_name = ?, email = ?, profile_completed_at = ?, "
            "email_signature = COALESCE(email_signature, ?), "
            "consented_tos_at = COALESCE(consented_tos_at, ?), "
            "consented_tos_version = COALESCE(consented_tos_version, ?), "
            "consented_privacy_at = COALESCE(consented_privacy_at, ?), "
            "consented_privacy_version = COALESCE(consented_privacy_version, ?) "
            "WHERE id = ?",
            (full_name, email, now, sig, now, TOS_VERSION, now, PRIVACY_VERSION, user_id),
        )
        await db.commit()


def default_signature_for(full_name: str) -> str:
    """The canonical starting signature we give every new account."""
    name = (full_name or "").strip() or "Your Name"
    return f"Cheers,\n\n{name}"


async def complete_email_profile(user_id: str, full_name: str, phone: str | None) -> None:
    """Finish profile for an email-mode signup (name + optional phone + consents).
    Email is already set on the user record from the signin step."""
    now = datetime.now(timezone.utc).isoformat()
    sig = default_signature_for(full_name)
    set_phone = phone is not None and phone != ""
    if set_phone:
        sql = (
            "UPDATE users SET full_name = ?, phone = ?, profile_completed_at = ?, "
            "email_signature = COALESCE(email_signature, ?), "
            "consented_tos_at = COALESCE(consented_tos_at, ?), "
            "consented_tos_version = COALESCE(consented_tos_version, ?), "
            "consented_privacy_at = COALESCE(consented_privacy_at, ?), "
            "consented_privacy_version = COALESCE(consented_privacy_version, ?) "
            "WHERE id = ?"
        )
        values = (full_name, phone, now, sig, now, TOS_VERSION, now, PRIVACY_VERSION, user_id)
    else:
        sql = (
            "UPDATE users SET full_name = ?, profile_completed_at = ?, "
            "email_signature = COALESCE(email_signature, ?), "
            "consented_tos_at = COALESCE(consented_tos_at, ?), "
            "consented_tos_version = COALESCE(consented_tos_version, ?), "
            "consented_privacy_at = COALESCE(consented_privacy_at, ?), "
            "consented_privacy_version = COALESCE(consented_privacy_version, ?) "
            "WHERE id = ?"
        )
        values = (full_name, now, sig, now, TOS_VERSION, now, PRIVACY_VERSION, user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, values)
        await db.commit()


async def update_email_settings(user_id: str, signature: str, branding_hidden: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET email_signature = ?, email_branding_hidden = ? WHERE id = ?",
            (signature or None, 1 if branding_hidden else 0, user_id),
        )
        await db.commit()


async def set_custom_email_domain(
    user_id: str, domain: str | None, domain_id: str | None, status: str | None
) -> None:
    """Persist the user's verified-domain record. Pass all None to clear."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET custom_email_domain = ?, "
            "custom_email_domain_id = ?, custom_email_domain_status = ? WHERE id = ?",
            (domain, domain_id, status, user_id),
        )
        await db.commit()


async def update_custom_email_domain_status(user_id: str, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET custom_email_domain_status = ? WHERE id = ?",
            (status, user_id),
        )
        await db.commit()


async def mark_login(user_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )
        await db.commit()


# ----- Sessions --------------------------------------------------------------

async def create_session(user_id: str, ip: str | None, ua: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SESSION_TTL_DAYS)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at, ip_address, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token, user_id, now.isoformat(), expires.isoformat(), ip, ua),
        )
        await db.commit()
    return token


async def get_session(token: str) -> dict | None:
    if not token:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT s.*, u.id AS user_id, u.phone, u.full_name, u.email, u.profile_completed_at, "
            "u.disabled_at, u.plan, u.stripe_customer_id, u.stripe_payment_method_id, "
            "u.custom_email_domain, u.custom_email_domain_id, u.custom_email_domain_status, "
            "u.email_signature, u.email_branding_hidden "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > ? AND u.disabled_at IS NULL",
            (token, datetime.now(timezone.utc).isoformat()),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_session(token: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        await db.commit()


# ----- Email OTP (fallback, we generate the code ourselves) -----------------

async def create_email_otp(phone: str, email: str) -> str:
    """Generate an email OTP, store hash, return the plain code to send."""
    code = generate_otp_code()
    code_h = hash_code(code)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=EMAIL_OTP_TTL_MINUTES)
    async with aiosqlite.connect(DB_PATH) as db:
        # Invalidate any older unused codes for this phone
        await db.execute(
            "UPDATE email_otp_codes SET used_at = ? WHERE phone = ? AND used_at IS NULL",
            (now.isoformat(), phone),
        )
        await db.execute(
            "INSERT INTO email_otp_codes (phone, email, code_hash, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (phone, email, code_h, expires.isoformat(), now.isoformat()),
        )
        await db.commit()
    return code


async def create_email_signin_otp(email: str) -> str:
    """Generate an email OTP keyed by email only (for AUTH_MODE=email signin)."""
    code = generate_otp_code()
    code_h = hash_code(code)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=EMAIL_OTP_TTL_MINUTES)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE email_signin_codes SET used_at = ? WHERE email = ? AND used_at IS NULL",
            (now.isoformat(), email),
        )
        await db.execute(
            "INSERT INTO email_signin_codes (email, code_hash, expires_at, created_at) "
            "VALUES (?, ?, ?, ?)",
            (email, code_h, expires.isoformat(), now.isoformat()),
        )
        await db.commit()
    return code


async def verify_email_signin_otp(email: str, code: str) -> bool:
    code_h = hash_code(code)
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, attempts FROM email_signin_codes "
            "WHERE email = ? AND used_at IS NULL AND expires_at > ? "
            "ORDER BY id DESC LIMIT 1",
            (email, now),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        if row["attempts"] >= EMAIL_OTP_MAX_ATTEMPTS:
            await db.execute("UPDATE email_signin_codes SET used_at = ? WHERE id = ?", (now, row["id"]))
            await db.commit()
            return False
        async with db.execute(
            "SELECT 1 FROM email_signin_codes WHERE id = ? AND code_hash = ?",
            (row["id"], code_h),
        ) as cur:
            match = await cur.fetchone()
        if match:
            await db.execute(
                "UPDATE email_signin_codes SET used_at = ?, attempts = attempts + 1 WHERE id = ?",
                (now, row["id"]),
            )
            await db.commit()
            return True
        else:
            await db.execute(
                "UPDATE email_signin_codes SET attempts = attempts + 1 WHERE id = ?",
                (row["id"],),
            )
            await db.commit()
            return False


async def verify_email_otp(phone: str, code: str) -> bool:
    code_h = hash_code(code)
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, attempts FROM email_otp_codes "
            "WHERE phone = ? AND used_at IS NULL AND expires_at > ? "
            "ORDER BY id DESC LIMIT 1",
            (phone, now),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        if row["attempts"] >= EMAIL_OTP_MAX_ATTEMPTS:
            # Too many wrong tries — invalidate
            await db.execute("UPDATE email_otp_codes SET used_at = ? WHERE id = ?", (now, row["id"]))
            await db.commit()
            return False
        # Check the code
        async with db.execute(
            "SELECT 1 FROM email_otp_codes WHERE id = ? AND code_hash = ?",
            (row["id"], code_h),
        ) as cur:
            match = await cur.fetchone()
        if match:
            await db.execute(
                "UPDATE email_otp_codes SET used_at = ?, attempts = attempts + 1 WHERE id = ?",
                (now, row["id"]),
            )
            await db.commit()
            return True
        else:
            await db.execute(
                "UPDATE email_otp_codes SET attempts = attempts + 1 WHERE id = ?",
                (row["id"],),
            )
            await db.commit()
            return False


# ----- FastAPI dependencies --------------------------------------------------

DESKTOP_MODE = os.getenv("VIDEOSCRIBER_DESKTOP") == "1"
DESKTOP_USER_ID = "desktop-local"


async def current_user(request: Request) -> dict | None:
    """Return the authenticated user dict, or None if no valid session.

    In desktop mode, the app is single-user and unauthenticated — we resolve
    every request to a synthetic "desktop-local" user seeded at startup.

    Caches the lookup on `request.state.user` so middleware + route dependencies
    don't each hit the DB.
    """
    cached = getattr(request.state, "user", "_unset")
    if cached != "_unset":
        return cached
    if DESKTOP_MODE:
        user = await get_user(DESKTOP_USER_ID)
        request.state.user = user
        return user
    token = request.cookies.get(SESSION_COOKIE)
    user = await get_session(token) if token else None
    request.state.user = user
    return user


async def require_user(request: Request) -> dict:
    """Dependency: return the authenticated user, or raise 401."""
    user = await current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


def redirect_to_login(path: str = "/login") -> RedirectResponse:
    return RedirectResponse(url=path, status_code=303)
