"""Custom email-domain routes (Plus plan only).

Plus users can send recap email from their own verified domain. We proxy the
domain through Resend's Domains API: the user adds a domain, we show the DNS
records, they add them to their registrar, then they click Verify to check.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request

import auth
import email_domains

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/email-domain", tags=["email-domain"])


def _require_plus(user: dict) -> None:
    if (user.get("plan") or "free") != "plus":
        raise HTTPException(
            status_code=402,
            detail="Custom email domains are a Plus feature. Upgrade to unlock.",
        )


async def _current_plus(request: Request) -> dict:
    user = await auth.current_user(request)
    if not user:
        raise HTTPException(401, "Not signed in")
    _require_plus(user)
    return user


def _serialize(user: dict, records: list[dict] | None = None) -> dict:
    return {
        "domain": user.get("custom_email_domain"),
        "domain_id": user.get("custom_email_domain_id"),
        "status": user.get("custom_email_domain_status") or (
            "none" if not user.get("custom_email_domain_id") else "pending"
        ),
        "records": records or [],
    }


@router.get("")
async def get_domain(user: dict = Depends(_current_plus)):
    """Return the user's current custom-domain state. If a domain_id exists,
    fetch fresh records from Resend so the DNS table stays accurate."""
    domain_id = user.get("custom_email_domain_id")
    if not domain_id:
        return _serialize(user)
    try:
        info = await email_domains.get_domain(domain_id)
    except Exception as e:
        logger.warning("Resend get_domain failed for %s: %s", domain_id, e)
        return _serialize(user)

    # Sync status if it drifted
    if info["status"] and info["status"] != user.get("custom_email_domain_status"):
        await auth.update_custom_email_domain_status(user["user_id"], info["status"])
        user["custom_email_domain_status"] = info["status"]
    return _serialize(user, records=info["records"])


@router.post("")
async def create_domain(
    domain: str = Form(...),
    user: dict = Depends(_current_plus),
):
    if user.get("custom_email_domain_id"):
        raise HTTPException(409, "You already have a domain configured. Remove it first.")
    try:
        clean = email_domains.normalize_domain(domain)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        info = await email_domains.create_domain(clean)
    except Exception as e:
        logger.warning("Resend create_domain failed for %s: %s", clean, e)
        raise HTTPException(502, "Couldn't add the domain. Please try again.")

    await auth.set_custom_email_domain(
        user["user_id"], clean, info["id"], info["status"] or "pending"
    )
    user.update({
        "custom_email_domain": clean,
        "custom_email_domain_id": info["id"],
        "custom_email_domain_status": info["status"] or "pending",
    })
    return _serialize(user, records=info["records"])


@router.post("/verify")
async def verify_domain(user: dict = Depends(_current_plus)):
    domain_id = user.get("custom_email_domain_id")
    if not domain_id:
        raise HTTPException(400, "No domain to verify. Add one first.")
    try:
        info = await email_domains.verify_domain(domain_id)
    except Exception as e:
        logger.warning("Resend verify_domain failed for %s: %s", domain_id, e)
        raise HTTPException(502, "Verification check failed. Please try again.")

    status = info["status"] or "pending"
    await auth.update_custom_email_domain_status(user["user_id"], status)
    user["custom_email_domain_status"] = status
    return _serialize(user, records=info["records"])


@router.delete("")
async def delete_domain(user: dict = Depends(_current_plus)):
    domain_id = user.get("custom_email_domain_id")
    if domain_id:
        await email_domains.delete_domain(domain_id)
    await auth.set_custom_email_domain(user["user_id"], None, None, None)
    user.update({
        "custom_email_domain": None,
        "custom_email_domain_id": None,
        "custom_email_domain_status": None,
    })
    return _serialize(user)
