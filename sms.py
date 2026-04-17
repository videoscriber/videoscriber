"""Twilio Verify wrapper for SMS OTP."""
import logging
import os

from fastapi import HTTPException
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_VERIFY_SERVICE_SID = os.getenv("TWILIO_VERIFY_SERVICE_SID")

# Bypass real SMS in dev — any phone accepts code "000000"
AUTH_DEV_BYPASS = os.getenv("AUTH_DEV_BYPASS", "").lower() in ("1", "true", "yes")
AUTH_DEV_CODE = "000000"

_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
            raise HTTPException(503, "SMS service is not configured")
        _client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _client


async def send_sms_otp(phone_e164: str) -> None:
    """Send an OTP via Twilio Verify. Raises HTTPException on failure."""
    if AUTH_DEV_BYPASS:
        logger.warning("[AUTH_DEV_BYPASS] Pretending to send SMS to %s — use code %s", phone_e164, AUTH_DEV_CODE)
        return
    if not TWILIO_VERIFY_SERVICE_SID:
        raise HTTPException(503, "SMS service is not configured")
    try:
        _get_client().verify.v2.services(TWILIO_VERIFY_SERVICE_SID).verifications.create(
            to=phone_e164, channel="sms"
        )
    except TwilioRestException as e:
        logger.warning("Twilio send failed for %s: %s", phone_e164, e)
        if e.code == 60200:
            raise HTTPException(400, "Invalid phone number")
        if e.code == 60203:
            raise HTTPException(429, "Too many attempts. Please wait before retrying.")
        if e.code == 60410:
            raise HTTPException(400, "This phone number is blocked by the carrier")
        raise HTTPException(502, "Could not send verification code. Please try again.")


async def verify_sms_otp(phone_e164: str, code: str) -> bool:
    """Check an OTP via Twilio Verify. Returns True on approval."""
    if AUTH_DEV_BYPASS:
        return code == AUTH_DEV_CODE
    if not TWILIO_VERIFY_SERVICE_SID:
        raise HTTPException(503, "SMS service is not configured")
    try:
        check = _get_client().verify.v2.services(TWILIO_VERIFY_SERVICE_SID).verification_checks.create(
            to=phone_e164, code=code
        )
        return check.status == "approved"
    except TwilioRestException as e:
        # 20404 = expired/not-found verification; treat as failed verification
        if e.code == 20404:
            return False
        logger.warning("Twilio verify failed for %s: %s", phone_e164, e)
        raise HTTPException(502, "Could not verify code. Please try again.")
