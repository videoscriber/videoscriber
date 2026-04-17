"""Resend wrapper for transactional email (OTP fallback + notifications)."""
import logging
import os

import resend
from fastapi import HTTPException

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
FROM_ADDRESS = os.getenv("RESEND_FROM", "VideoScriber <auth@videoscriber.ai>")

AUTH_DEV_BYPASS = os.getenv("AUTH_DEV_BYPASS", "").lower() in ("1", "true", "yes")

_configured = False


def _ensure_configured() -> None:
    global _configured
    if _configured:
        return
    if not RESEND_API_KEY:
        raise HTTPException(503, "Email service is not configured")
    resend.api_key = RESEND_API_KEY
    _configured = True


async def send_email_signin_code(to_email: str, code: str) -> None:
    """Send an OTP via email for the email-primary signin flow (AUTH_MODE=email).
    Copy is tailored to primary signin (not a fallback)."""
    if AUTH_DEV_BYPASS or not RESEND_API_KEY:
        logger.warning("[AUTH_DEV_BYPASS] Pretending to email %s — signin code is %s", to_email, code)
        return
    _ensure_configured()
    subject = f"Your VideoScriber sign-in code: {code}"
    html = f"""\
<!doctype html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #09090b; color: #f0f0f5; padding: 40px 20px;">
  <div style="max-width: 480px; margin: 0 auto; background: #131318; border-radius: 14px; padding: 32px; border: 1px solid rgba(255,255,255,0.06);">
    <h1 style="margin: 0 0 16px; font-size: 20px; color: #f0f0f5;">Your VideoScriber sign-in code</h1>
    <p style="margin: 0 0 24px; color: #9494a8; font-size: 14px;">
      Enter this code to sign in or create your account:
    </p>
    <div style="background: #1a1a22; border-radius: 10px; padding: 20px; text-align: center; margin: 24px 0;">
      <div style="font-size: 32px; font-weight: 600; letter-spacing: 8px; color: #a78bfa; font-family: 'SF Mono', 'Fira Code', monospace;">
        {code}
      </div>
    </div>
    <p style="margin: 24px 0 0; color: #5c5c72; font-size: 12px;">
      This code expires in 10 minutes. If you didn't request it, you can safely ignore this email.
    </p>
  </div>
</body></html>"""
    text = f"Your VideoScriber sign-in code is {code}. It expires in 10 minutes."
    try:
        resend.Emails.send({
            "from": FROM_ADDRESS,
            "to": [to_email],
            "subject": subject,
            "html": html,
            "text": text,
        })
    except Exception as e:
        logger.warning("Resend signin send failed for %s: %s", to_email, e)
        raise HTTPException(502, "Could not send email. Please try again.")


async def send_email_otp(to_email: str, code: str, masked_phone: str) -> None:
    """Send an OTP code via email as a fallback to SMS."""
    if AUTH_DEV_BYPASS or not RESEND_API_KEY:
        logger.warning("[AUTH_DEV_BYPASS] Pretending to email %s — code is %s", to_email, code)
        return
    _ensure_configured()
    subject = f"Your VideoScriber sign-in code: {code}"
    html = f"""\
<!doctype html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #09090b; color: #f0f0f5; padding: 40px 20px;">
  <div style="max-width: 480px; margin: 0 auto; background: #131318; border-radius: 14px; padding: 32px; border: 1px solid rgba(255,255,255,0.06);">
    <h1 style="margin: 0 0 16px; font-size: 20px; color: #f0f0f5;">VideoScriber sign-in code</h1>
    <p style="margin: 0 0 24px; color: #9494a8; font-size: 14px;">
      You requested a sign-in code as a backup for your phone {masked_phone}. Enter this code to continue:
    </p>
    <div style="background: #1a1a22; border-radius: 10px; padding: 20px; text-align: center; margin: 24px 0;">
      <div style="font-size: 32px; font-weight: 600; letter-spacing: 8px; color: #a78bfa; font-family: 'SF Mono', 'Fira Code', monospace;">
        {code}
      </div>
    </div>
    <p style="margin: 24px 0 0; color: #5c5c72; font-size: 12px;">
      This code expires in 10 minutes. If you didn't request it, you can safely ignore this email.
    </p>
  </div>
</body></html>"""
    text = f"Your VideoScriber sign-in code is {code}. It expires in 10 minutes."
    try:
        resend.Emails.send({
            "from": FROM_ADDRESS,
            "to": [to_email],
            "subject": subject,
            "html": html,
            "text": text,
        })
    except Exception as e:
        logger.warning("Resend send failed for %s: %s", to_email, e)
        raise HTTPException(502, "Could not send email. Please try again.")
