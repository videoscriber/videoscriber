"""Zoom integration: OAuth flow + Cloud Recordings API client.

The user-facing flow:
  1. User clicks "Connect" in Settings → backend redirects them to
     build_authorize_url(user_id) on zoom.us.
  2. Zoom sends them back to /api/integrations/zoom/oauth/callback with a
     `code` and `state`; exchange_code() trades the code for tokens,
     fetch_me() labels the integration with the user's email, and
     integrations_routes persists both in the `integrations` table with
     tokens Fernet-encrypted.
  3. When the user clicks "Import now", list_recordings() pulls their
     cloud recordings and download_recording_file() fetches the MP4 that
     gets handed to the transcription pipeline.

State parameter (CSRF + session binding):
  We sign `user_id|nonce|timestamp` with HMAC-SHA256 using OTP_HASH_KEY
  (already used for OTP codes). Self-validating so we don't need to
  persist pending-OAuth state in the DB.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

ZOOM_AUTHORIZE_URL = "https://zoom.us/oauth/authorize"
ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
ZOOM_API_BASE = "https://api.zoom.us/v2"

# State parameter lifetime: short enough that a dropped OAuth attempt can't be
# replayed days later, long enough to survive a slow MFA prompt on Zoom's side.
STATE_MAX_AGE_S = 600  # 10 minutes


def _env_required(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(f"{key} is not set — Zoom integration cannot be used.")
    return value


def is_configured() -> bool:
    """True once the Zoom OAuth credentials are present. Mirrors the check
    in integrations_routes.PROVIDER_CATALOG so the Settings UI stays honest."""
    return all(os.getenv(k, "").strip() for k in (
        "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_REDIRECT_URI",
    ))


# ---------------------------------------------------------------------------
# State parameter (signed, self-validating)
# ---------------------------------------------------------------------------
def _state_key() -> bytes:
    """HMAC key for OAuth state. Reuses OTP_HASH_KEY since it's already
    required in the env; the domain separator in the payload keeps the two
    use-cases from overlapping."""
    key = os.getenv("OTP_HASH_KEY", "").strip()
    if not key:
        raise RuntimeError("OTP_HASH_KEY is required for Zoom OAuth state signing.")
    return key.encode("utf-8")


def _make_state(user_id: str) -> str:
    nonce = secrets.token_urlsafe(12)
    timestamp = str(int(time.time()))
    payload = f"zoom-oauth|{user_id}|{nonce}|{timestamp}"
    sig = hmac.new(_state_key(), payload.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    return f"{base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')}.{sig_b64}"


def parse_state(state: str) -> str:
    """Verify a signed state and return the user_id that initiated the flow.
    Raises ValueError if the signature is bad or the state is expired."""
    try:
        payload_b64, sig_b64 = state.split(".", 1)
    except ValueError:
        raise ValueError("Malformed state parameter")

    def _b64pad(s: str) -> str:
        return s + "=" * (-len(s) % 4)

    try:
        payload = base64.urlsafe_b64decode(_b64pad(payload_b64)).decode("utf-8")
        sig_bytes = base64.urlsafe_b64decode(_b64pad(sig_b64))
    except Exception:
        raise ValueError("Corrupt state parameter")

    expected = hmac.new(_state_key(), payload.encode("utf-8"), hashlib.sha256).digest()
    if not hmac.compare_digest(sig_bytes, expected):
        raise ValueError("Invalid state signature")

    parts = payload.split("|")
    if len(parts) != 4 or parts[0] != "zoom-oauth":
        raise ValueError("Unexpected state payload")

    _, user_id, _nonce, ts = parts
    try:
        ts_int = int(ts)
    except ValueError:
        raise ValueError("Unparseable state timestamp")
    if int(time.time()) - ts_int > STATE_MAX_AGE_S:
        raise ValueError("State parameter expired")
    return user_id


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------
def build_authorize_url(user_id: str) -> str:
    """Build the URL we redirect the user to to start the OAuth dance."""
    client_id = _env_required("ZOOM_CLIENT_ID")
    redirect_uri = _env_required("ZOOM_REDIRECT_URI")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": _make_state(user_id),
    }
    return f"{ZOOM_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict[str, Any]:
    """Swap an OAuth `code` for an access-token bundle.

    Returns the parsed JSON response from Zoom:
      {
        "access_token": "...",      # expires in `expires_in` seconds (~1h)
        "token_type": "bearer",
        "refresh_token": "...",     # long-lived, use to mint new access tokens
        "expires_in": 3600,
        "scope": "cloud_recording:read:...",
      }
    """
    client_id = _env_required("ZOOM_CLIENT_ID")
    client_secret = _env_required("ZOOM_CLIENT_SECRET")
    redirect_uri = _env_required("ZOOM_REDIRECT_URI")

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            ZOOM_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            auth=(client_id, client_secret),
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        logger.warning("Zoom token exchange failed: %s %s", resp.status_code, resp.text[:500])
        raise RuntimeError(f"Zoom token exchange failed: {resp.status_code}")
    return resp.json()


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Trade a refresh token for a new access token. Zoom rotates refresh
    tokens on every refresh — callers MUST persist the new refresh_token."""
    client_id = _env_required("ZOOM_CLIENT_ID")
    client_secret = _env_required("ZOOM_CLIENT_SECRET")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            ZOOM_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(client_id, client_secret),
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        logger.warning("Zoom refresh failed: %s %s", resp.status_code, resp.text[:500])
        raise RuntimeError(f"Zoom refresh failed: {resp.status_code}")
    return resp.json()


# ---------------------------------------------------------------------------
# Cloud Recordings API
# ---------------------------------------------------------------------------
async def fetch_me(access_token: str) -> dict[str, Any]:
    """Fetch the current user's profile so we can label the integration."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{ZOOM_API_BASE}/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    resp.raise_for_status()
    return resp.json()


async def list_recordings(
    access_token: str,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    page_size: int = 30,
    next_page_token: str | None = None,
) -> dict[str, Any]:
    """List the authorized user's cloud recordings. Zoom defaults the date
    range to the last month; callers can pass ISO `YYYY-MM-DD` for `from` and
    `to` to widen it."""
    params: dict[str, Any] = {"page_size": min(page_size, 300)}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    if next_page_token:
        params["next_page_token"] = next_page_token

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{ZOOM_API_BASE}/users/me/recordings",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
    resp.raise_for_status()
    return resp.json()


def pick_primary_video_file(recording: dict[str, Any]) -> dict[str, Any] | None:
    """Choose the best single MP4 to transcribe from a Zoom recording. Zoom
    usually ships several files per meeting — shared-screen-with-speaker,
    gallery-view, audio-only, chat-file. We want the one that has both the
    speakers and the screen, falling back to any MP4 video, and finally the
    M4A audio track if video isn't available."""
    files = recording.get("recording_files") or []
    if not files:
        return None
    mp4 = [f for f in files if (f.get("file_type") or "").upper() == "MP4"]
    preferred = [
        f for f in mp4
        if (f.get("recording_type") or "") == "shared_screen_with_speaker_view"
    ]
    if preferred:
        return preferred[0]
    if mp4:
        return mp4[0]
    m4a = [f for f in files if (f.get("file_type") or "").upper() == "M4A"]
    return m4a[0] if m4a else None


async def download_recording_file(
    file_meta: dict[str, Any],
    access_token: str,
    dest_path: str,
) -> int:
    """Stream a recording file from Zoom to `dest_path`. Returns the number
    of bytes written. Zoom accepts the access token either via the standard
    `Authorization: Bearer` header or a `?access_token=...` query parameter;
    we use the header so the token never hits server logs as a URL param."""
    download_url = file_meta.get("download_url")
    if not download_url:
        raise ValueError("Recording file has no download_url")

    bytes_written = 0
    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        async with client.stream(
            "GET",
            download_url,
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
                        bytes_written += len(chunk)
    return bytes_written
