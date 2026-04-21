"""Google Meet integration — OAuth + Drive-backed recording list.

Google Meet stores cloud recordings as MP4 files in the authenticated user's
Drive, typically under a "Meet Recordings" folder. We use the Drive v3 API
with a narrow `drive.readonly` scope rather than the newer Meet REST API
because:

  - Drive coverage is older and more stable; Meet API recordings endpoint
    has edge cases with who owns what.
  - A single search query (files.list with a MIME-type + folder filter)
    returns exactly what we need with fresh download URLs.
  - `drive.readonly` is already a well-understood restricted scope for
    consent-screen purposes.

Flow mirrors zoom_provider.py on purpose so integrations_routes.py can
layer the same OAuth + picker + import pattern with near-zero divergence.
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

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

# Scopes:
#   drive.readonly — read-only access to Drive files (tight enough for us)
#   openid + email + profile — label the integration with the user's email
#
# `drive.readonly` is a "restricted scope" in Google's terms. Listing pages
# need a security assessment once the app moves past 100 users, but we're
# in beta territory so unverified is fine for now.
GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/drive.readonly",
]

STATE_MAX_AGE_S = 600  # same 10-minute window as Zoom


def _env_required(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(f"{key} is not set — Google Meet integration cannot be used.")
    return value


def is_configured() -> bool:
    """True once the Google OAuth credentials are present."""
    return all(
        os.getenv(k, "").strip()
        for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI")
    )


# ---------------------------------------------------------------------------
# State parameter — same HMAC scheme as Zoom, just a different domain tag.
# ---------------------------------------------------------------------------
def _state_key() -> bytes:
    key = os.getenv("OTP_HASH_KEY", "").strip()
    if not key:
        raise RuntimeError("OTP_HASH_KEY is required for Google OAuth state signing.")
    return key.encode("utf-8")


def _make_state(user_id: str) -> str:
    nonce = secrets.token_urlsafe(12)
    timestamp = str(int(time.time()))
    payload = f"google-oauth|{user_id}|{nonce}|{timestamp}"
    sig = hmac.new(_state_key(), payload.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    return f"{base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')}.{sig_b64}"


def parse_state(state: str) -> str:
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
    if len(parts) != 4 or parts[0] != "google-oauth":
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
    """Build the URL we redirect the user to to start OAuth.

    `access_type=offline` is required to receive a refresh token.
    `prompt=consent` forces the consent screen on every connect so Google
    always hands back a refresh token — without it, subsequent re-connects
    only return an access token and refresh_token goes missing."""
    client_id = _env_required("GOOGLE_CLIENT_ID")
    redirect_uri = _env_required("GOOGLE_REDIRECT_URI")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(GOOGLE_SCOPES),
        "state": _make_state(user_id),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict[str, Any]:
    """Trade the OAuth code for tokens.

    Google token endpoint expects `client_id` + `client_secret` in the body
    (unlike Zoom, which uses HTTP Basic). The response is the same shape
    though: access_token + refresh_token + expires_in."""
    client_id = _env_required("GOOGLE_CLIENT_ID")
    client_secret = _env_required("GOOGLE_CLIENT_SECRET")
    redirect_uri = _env_required("GOOGLE_REDIRECT_URI")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        logger.warning("Google token exchange failed: %s %s", resp.status_code, resp.text[:500])
        raise RuntimeError(f"Google token exchange failed: {resp.status_code}")
    return resp.json()


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Swap a refresh token for a fresh access token. Google does NOT rotate
    refresh tokens by default, so callers can persist the same refresh_token
    forever — but we still accept whatever comes back just in case."""
    client_id = _env_required("GOOGLE_CLIENT_ID")
    client_secret = _env_required("GOOGLE_CLIENT_SECRET")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        logger.warning("Google refresh failed: %s %s", resp.status_code, resp.text[:500])
        raise RuntimeError(f"Google refresh failed: {resp.status_code}")
    data = resp.json()
    # Google omits refresh_token on refresh responses; preserve the original
    # so callers can just spread the dict over existing state.
    data.setdefault("refresh_token", refresh_token)
    return data


# ---------------------------------------------------------------------------
# Drive API — listing Meet recordings
# ---------------------------------------------------------------------------
async def fetch_userinfo(access_token: str) -> dict[str, Any]:
    """Grab the authenticated user's email + name to label the integration."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    resp.raise_for_status()
    return resp.json()


def _build_meet_query(
    from_date: str | None,
    to_date: str | None,
) -> str:
    """Drive query string: MP4s in a Meet Recordings folder, not trashed.

    Meet drops recordings into an auto-created "Meet Recordings" folder in
    the user's My Drive. We match by folder name + MP4 mime-type — simple
    and matches how both Google and every third-party integration describes
    these files."""
    clauses = [
        "mimeType = 'video/mp4'",
        "trashed = false",
        # Folder membership check: recording lives in a folder named
        # "Meet Recordings" (Google's default). The 'parents' filter is a
        # sub-query — we just require membership in *any* parent named
        # Meet Recordings.
        "'me' in owners",
    ]
    if from_date:
        clauses.append(f"createdTime >= '{from_date}T00:00:00'")
    if to_date:
        clauses.append(f"createdTime <= '{to_date}T23:59:59'")
    return " and ".join(clauses)


async def list_meet_recordings(
    access_token: str,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    page_size: int = 50,
    page_token: str | None = None,
) -> dict[str, Any]:
    """Return Drive files that look like Meet recordings. Matches MP4s the
    user owns, optionally filtered by the createdTime range. We narrow to
    the 'Meet Recordings' folder by post-filtering the Drive response on
    `parents` → folder name; an up-front `and '...' in parents` clause
    would require looking up the folder id first, which adds a round-trip.

    If no from/to is supplied, default to the last 6 months — same policy
    as the Zoom picker."""
    if not (from_date or to_date):
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        today = _dt.now(_tz.utc).date()
        from_date = (today - _td(days=180)).isoformat()
        to_date = today.isoformat()

    params = {
        "q": _build_meet_query(from_date, to_date),
        "pageSize": min(page_size, 100),
        "fields": (
            "nextPageToken,files("
            "id,name,mimeType,size,createdTime,modifiedTime,"
            "videoMediaMetadata,parents,webViewLink)"
        ),
        "orderBy": "createdTime desc",
        # Needed to see files in Shared Drives the user has access to.
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if page_token:
        params["pageToken"] = page_token

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{DRIVE_API_BASE}/files",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
    resp.raise_for_status()
    data = resp.json()

    # Narrow to files whose parent folder is named "Meet Recordings". We
    # have to look that folder's name up — do one batch lookup for all
    # unique parent ids in the response.
    parent_ids = set()
    for f in data.get("files") or []:
        for p in f.get("parents") or []:
            parent_ids.add(p)
    parent_names: dict[str, str] = {}
    if parent_ids:
        # Small-batch name lookups (Drive doesn't offer bulk).
        async with httpx.AsyncClient(timeout=30.0) as client:
            for pid in parent_ids:
                r = await client.get(
                    f"{DRIVE_API_BASE}/files/{pid}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"fields": "id,name", "supportsAllDrives": "true"},
                )
                if r.status_code == 200:
                    parent_names[pid] = (r.json().get("name") or "").strip()

    meet_files = []
    for f in data.get("files") or []:
        parents = f.get("parents") or []
        if any((parent_names.get(p) or "").lower() == "meet recordings" for p in parents):
            meet_files.append(f)

    return {"files": meet_files, "nextPageToken": data.get("nextPageToken") or None}


async def download_drive_file(
    file_id: str,
    access_token: str,
    dest_path: str,
) -> int:
    """Stream a Drive file to disk with alt=media. Returns bytes written."""
    bytes_written = 0
    url = f"{DRIVE_API_BASE}/files/{file_id}"
    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        async with client.stream(
            "GET",
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"alt": "media", "supportsAllDrives": "true"},
        ) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
                        bytes_written += len(chunk)
    return bytes_written
