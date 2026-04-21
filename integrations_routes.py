"""Integrations: Zoom, Google Meet, and local folder sources that feed the
transcription pipeline.

Phase 1 (this module) ships the schema and CRUD surface: list/connect/
disconnect/update-mode endpoints and recent-import history. OAuth flows and
per-provider sync workers land in Phase 2 (Zoom), Phase 3 (Google), and
Phase 5 (auto-sync).

Design notes
- Three providers share a single table (`integrations`). `provider` is a
  discriminator; provider-specific data (folder path, scope filters) lives
  in `settings_json`.
- `sync_mode` is the user-facing toggle: 'off' | 'manual' | 'auto'.
  'manual' (picker-only) is available to everyone. 'auto' is Plus-only.
- Access + refresh tokens are stored encrypted. The encryption key is
  INTEGRATIONS_TOKEN_KEY (base64-encoded Fernet key). Without the key
  `_encrypt`/`_decrypt` raise so we never silently write plaintext secrets.
"""
import asyncio
import base64
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse

import auth
import database as db
import zoom_provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/integrations", tags=["integrations"])

# Supported providers. Anything not in this set returns 400 from public routes
# so callers can't create arbitrary rows.
SUPPORTED_PROVIDERS = {"zoom", "google_meet", "local_folder"}
VALID_SYNC_MODES = {"off", "manual", "auto"}

# Pretty labels + capability flags used by the Settings UI. Kept here so the
# frontend has a single authoritative source; the /status endpoint echoes it.
PROVIDER_CATALOG = {
    "zoom": {
        "label": "Zoom",
        "description": "Import your Zoom Cloud Recordings.",
        "requires_oauth": True,
        # True once every ZOOM_* env var is set — keeps the catalog in lock-step
        # with zoom_provider.is_configured() so the Settings UI can't claim a
        # provider is live while one of the OAuth knobs is still missing.
        "configured": zoom_provider.is_configured(),
    },
    "google_meet": {
        "label": "Google Meet",
        "description": "Import Meet recordings from your Google Drive.",
        "requires_oauth": True,
        "configured": bool(os.getenv("GOOGLE_CLIENT_ID")),
    },
    "local_folder": {
        "label": "Local folder",
        "description": "Auto-upload new videos dropped into a folder on your machine.",
        "requires_oauth": False,
        # Desktop-only — the browser version has no persistent fs access.
        "configured": True,
        "desktop_only": True,
    },
}


# ---------------------------------------------------------------------------
# Token encryption helpers. `Fernet` is tiny and in `cryptography`, which is
# already a transitive dep of weasyprint. Importing lazily so unit tests that
# don't touch encryption don't need the env var present.
# ---------------------------------------------------------------------------
def _fernet():
    from cryptography.fernet import Fernet  # type: ignore

    key = os.getenv("INTEGRATIONS_TOKEN_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "INTEGRATIONS_TOKEN_KEY is not set. Generate one with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'` and add it to .env."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def _encrypt(value: str | None) -> bytes | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode("utf-8"))


def _decrypt(value: bytes | None) -> str | None:
    if not value:
        return None
    return _fernet().decrypt(value).decode("utf-8")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def _public_integration(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a DB row for the API client — no secret fields, parsed settings.
    Always returns the catalog metadata alongside so the UI has one payload."""
    catalog = PROVIDER_CATALOG.get(row["provider"], {})
    settings = {}
    if row.get("settings_json"):
        try:
            settings = json.loads(row["settings_json"])
        except json.JSONDecodeError:
            settings = {}
    return {
        "id": row["id"],
        "provider": row["provider"],
        "provider_label": catalog.get("label", row["provider"]),
        "account_label": row.get("account_label"),
        "sync_mode": row.get("sync_mode") or "manual",
        "settings": settings,
        "last_sync_at": row.get("last_sync_at"),
        "last_sync_status": row.get("last_sync_status"),
        "last_sync_error": row.get("last_sync_error"),
        "token_expires_at": row.get("token_expires_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _provider_card(provider: str, integration: dict | None) -> dict:
    """One card's worth of state: what the Settings UI renders per-provider.
    If the user hasn't connected this provider yet, `integration` is null and
    the UI shows a Connect button."""
    meta = PROVIDER_CATALOG[provider]
    return {
        "provider": provider,
        "label": meta["label"],
        "description": meta["description"],
        "requires_oauth": meta["requires_oauth"],
        "configured": meta["configured"],
        "desktop_only": meta.get("desktop_only", False),
        "integration": _public_integration(integration) if integration else None,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/status")
async def integrations_status(user: dict = Depends(auth.require_user)):
    """Everything the Settings UI needs in one shot: the per-provider catalog
    cards with the user's connection state merged in."""
    rows = await db.list_integrations(user["user_id"])
    by_provider = {r["provider"]: r for r in rows}
    cards = [
        _provider_card(p, by_provider.get(p))
        for p in ("zoom", "google_meet", "local_folder")
    ]
    plan = user.get("plan") or "free"
    return {
        "plan": plan,
        "auto_sync_available": plan == "plus",
        "cards": cards,
    }


@router.get("/{integration_id}")
async def get_integration(integration_id: str, user: dict = Depends(auth.require_user)):
    row = await db.get_integration(integration_id, user["user_id"])
    if not row:
        raise HTTPException(404, "Integration not found")
    return _public_integration(row)


@router.get("/{integration_id}/imports")
async def recent_imports(integration_id: str, user: dict = Depends(auth.require_user)):
    row = await db.get_integration(integration_id, user["user_id"])
    if not row:
        raise HTTPException(404, "Integration not found")
    items = await db.list_integration_imports(integration_id, limit=25)
    return {"imports": items}


@router.patch("/{integration_id}")
async def update_integration(
    integration_id: str,
    sync_mode: str = Form(default=""),
    settings: str = Form(default=""),
    user: dict = Depends(auth.require_user),
):
    """Toggle sync_mode or update provider-specific settings (e.g. folder path
    for the local_folder provider). Plus gate lives here: free users can't
    flip to 'auto'."""
    row = await db.get_integration(integration_id, user["user_id"])
    if not row:
        raise HTTPException(404, "Integration not found")

    next_mode = (sync_mode or row["sync_mode"] or "manual").strip()
    if next_mode not in VALID_SYNC_MODES:
        raise HTTPException(400, f"sync_mode must be one of {sorted(VALID_SYNC_MODES)}")
    if next_mode == "auto" and (user.get("plan") or "free") != "plus":
        raise HTTPException(402, "Auto-sync is a Plus feature. Upgrade to enable it.")

    settings_json = None
    if settings:
        try:
            parsed = json.loads(settings)
            if not isinstance(parsed, dict):
                raise ValueError("settings must be a JSON object")
            settings_json = json.dumps(parsed)
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(400, f"Invalid settings JSON: {e}")

    await db.update_integration_sync_state(
        integration_id,
        sync_mode=next_mode,
        settings_json=settings_json,
    )
    updated = await db.get_integration(integration_id, user["user_id"])
    return _public_integration(updated)


@router.delete("/{integration_id}")
async def disconnect_integration(
    integration_id: str,
    user: dict = Depends(auth.require_user),
):
    row = await db.get_integration(integration_id, user["user_id"])
    if not row:
        raise HTTPException(404, "Integration not found")
    await db.delete_integration(integration_id, user["user_id"])
    logger.info("Integration %s (%s) disconnected by user %s",
                integration_id, row["provider"], user["user_id"])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Local-folder provider — connect/update without OAuth. The desktop app calls
# this when the user picks a folder; the server just records the path and
# flips sync_mode. Actual file-watching happens in Electron (Phase 4).
# ---------------------------------------------------------------------------
@router.post("/local_folder/connect")
async def connect_local_folder(
    folder_path: str = Form(...),
    user: dict = Depends(auth.require_user),
):
    folder_path = folder_path.strip()
    if not folder_path:
        raise HTTPException(400, "folder_path is required")

    integration_id = str(uuid.uuid4())
    await db.upsert_integration(
        integration_id,
        user["user_id"],
        "local_folder",
        account_label=folder_path,
        settings_json=json.dumps({"folder_path": folder_path}),
        sync_mode="manual",
    )
    row = await db.get_integration_by_provider(user["user_id"], "local_folder")
    return _public_integration(row)


# ---------------------------------------------------------------------------
# OAuth providers — Phase 2/3 wiring. The `connect` route returns a
# `{redirect}` URL that the frontend navigates to; Zoom sends the user back
# to the provider-specific `/oauth/callback` route below.
# ---------------------------------------------------------------------------
@router.post("/{provider}/connect")
async def connect_oauth_provider(
    provider: str,
    user: dict = Depends(auth.require_user),
):
    if provider == "local_folder":
        raise HTTPException(400, "Use POST /api/integrations/local_folder/connect with folder_path")
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"Unknown provider: {provider}")
    if not PROVIDER_CATALOG[provider]["configured"]:
        raise HTTPException(
            503,
            f"{PROVIDER_CATALOG[provider]['label']} isn't set up yet. "
            "OAuth credentials still need to be configured on the server.",
        )

    if provider == "zoom":
        return {"redirect": zoom_provider.build_authorize_url(user["user_id"])}

    # Phase 3 adds google_meet.
    raise HTTPException(501, "OAuth flow not yet implemented for this provider.")


# ---------------------------------------------------------------------------
# Zoom OAuth callback + API surface
# ---------------------------------------------------------------------------
def _zoom_expiry(expires_in: int | None) -> str:
    """Zoom returns a relative `expires_in` in seconds; we persist an absolute
    ISO timestamp for the short-lived access token, subtracting a safety
    margin so refreshes happen before the token technically expires."""
    seconds = max(60, int(expires_in or 3600) - 60)
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


async def _save_zoom_tokens(
    integration_id: str,
    user_id: str,
    account_label: str | None,
    token_bundle: dict,
) -> None:
    """Persist Zoom tokens encrypted + label + expiry. Shared by the
    initial callback and the refresh path."""
    await db.upsert_integration(
        integration_id,
        user_id,
        "zoom",
        account_label=account_label,
        access_token_encrypted=_encrypt(token_bundle.get("access_token")),
        refresh_token_encrypted=_encrypt(token_bundle.get("refresh_token")),
        token_expires_at=_zoom_expiry(token_bundle.get("expires_in")),
        sync_mode="manual",
    )


async def _fresh_zoom_access_token(row: dict) -> str:
    """Return a usable Zoom access token. If the stored one is close to
    expiry we refresh it first and write the new refresh+access pair back
    to the integrations row (Zoom rotates refresh tokens on every refresh)."""
    expires_at = row.get("token_expires_at")
    needs_refresh = True
    if expires_at:
        try:
            when = datetime.fromisoformat(expires_at)
            needs_refresh = when <= datetime.now(timezone.utc) + timedelta(seconds=30)
        except ValueError:
            needs_refresh = True

    if not needs_refresh:
        return _decrypt(row["access_token_encrypted"])

    refresh_token = _decrypt(row["refresh_token_encrypted"])
    if not refresh_token:
        raise HTTPException(400, "Zoom connection is missing a refresh token — reconnect.")
    try:
        bundle = await zoom_provider.refresh_access_token(refresh_token)
    except Exception as e:
        logger.warning("Zoom token refresh failed for integration %s: %s", row["id"], e)
        raise HTTPException(401, "Zoom token refresh failed. Disconnect and reconnect Zoom.")

    await _save_zoom_tokens(row["id"], row["user_id"], row.get("account_label"), bundle)
    return bundle["access_token"]


@router.get("/zoom/oauth/callback")
async def zoom_oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    """Zoom redirects here after the user authorizes (or cancels). We verify
    the state, exchange the code for tokens, fetch the user's email to label
    the integration, and bounce back to Settings with a status indicator."""
    # Unauthenticated entrypoint — the middleware exempts /api/integrations
    # paths only when they don't require a session. Since this route is
    # triggered by the browser redirect (no cookie reliability across
    # domains), we re-authenticate using the signed state parameter.
    if error:
        logger.info("Zoom authorization declined: %s", error)
        return RedirectResponse("/app/settings?integration=zoom&status=cancelled", status_code=303)
    if not code or not state:
        raise HTTPException(400, "Missing code or state from Zoom")

    try:
        user_id = zoom_provider.parse_state(state)
    except ValueError as e:
        logger.warning("Zoom OAuth state rejected: %s", e)
        raise HTTPException(400, "Invalid or expired authorization state.")

    try:
        bundle = await zoom_provider.exchange_code(code)
    except RuntimeError as e:
        logger.warning("Zoom code exchange failed: %s", e)
        return RedirectResponse(
            "/app/settings?integration=zoom&status=error",
            status_code=303,
        )

    access_token = bundle.get("access_token")
    if not access_token:
        raise HTTPException(502, "Zoom returned no access token")

    try:
        me = await zoom_provider.fetch_me(access_token)
    except Exception as e:
        logger.warning("Zoom /users/me failed: %s", e)
        me = {}
    account_label = me.get("email") or me.get("display_name") or "Zoom"

    existing = await db.get_integration_by_provider(user_id, "zoom")
    integration_id = existing["id"] if existing else str(uuid.uuid4())
    await _save_zoom_tokens(integration_id, user_id, account_label, bundle)
    logger.info("Zoom connected for user=%s, account=%s", user_id, account_label)

    return RedirectResponse(
        "/app/settings?integration=zoom&status=connected",
        status_code=303,
    )


@router.get("/zoom/recordings")
async def list_zoom_recordings(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    next_page_token: str | None = Query(default=None),
    user: dict = Depends(auth.require_user),
):
    """List the authenticated user's Zoom Cloud Recordings. Caller opens the
    picker in Settings or the upload area and chooses which to import."""
    row = await db.get_integration_by_provider(user["user_id"], "zoom")
    if not row:
        raise HTTPException(404, "Zoom is not connected for this account.")
    access_token = await _fresh_zoom_access_token(row)
    try:
        data = await zoom_provider.list_recordings(
            access_token,
            from_date=from_date,
            to_date=to_date,
            next_page_token=next_page_token,
        )
    except httpx.HTTPStatusError as e:  # type: ignore[name-defined]  # imported lazily below
        logger.warning("Zoom list_recordings failed: %s %s", e.response.status_code, e.response.text[:500])
        raise HTTPException(502, "Zoom API error")

    # Flatten to the fields the picker actually needs.
    items = []
    for meeting in data.get("meetings") or []:
        primary = zoom_provider.pick_primary_video_file(meeting)
        items.append({
            "uuid": meeting.get("uuid"),
            "topic": meeting.get("topic"),
            "start_time": meeting.get("start_time"),
            "duration_minutes": meeting.get("duration"),
            "total_size": meeting.get("total_size"),
            "has_video": bool(primary and (primary.get("file_type") or "").upper() == "MP4"),
            "file_id": primary.get("id") if primary else None,
        })
    return {
        "items": items,
        "next_page_token": data.get("next_page_token") or None,
    }


@router.post("/zoom/import")
async def import_zoom_recording(
    background: BackgroundTasks,
    recording_uuid: str = Form(...),
    user: dict = Depends(auth.require_user),
):
    """Kick off an import of a specific Zoom recording. The download +
    transcription run as a background task; the picker dismisses
    immediately and the job appears in the user's library with its normal
    'pending → transcribing → done' lifecycle.

    Implementation note: we re-find the meeting via /users/me/recordings
    instead of /meetings/{uuid}/recordings. The per-meeting endpoint
    requires `cloud_recording:read:list_recording_files`, while the list
    endpoint only needs the `cloud_recording:read:list_user_recordings`
    scope we already have. The list endpoint also returns fresh
    download_urls, so there's no downside to the extra filtering."""
    row = await db.get_integration_by_provider(user["user_id"], "zoom")
    if not row:
        raise HTTPException(404, "Zoom is not connected for this account.")
    access_token = await _fresh_zoom_access_token(row)

    # Widen the date range to 2 years — the user already saw this recording
    # in the picker so it exists somewhere in their account. One listing is
    # usually enough; we paginate only if we don't find the UUID on page 1.
    from datetime import date as _date, timedelta as _td
    today = _date.today().isoformat()
    two_years_ago = (_date.today() - _td(days=730)).isoformat()

    meeting: dict | None = None
    next_page: str | None = None
    for _ in range(20):  # hard cap ~6000 meetings — generous
        data = await zoom_provider.list_recordings(
            access_token,
            from_date=two_years_ago,
            to_date=today,
            page_size=300,
            next_page_token=next_page,
        )
        for m in data.get("meetings") or []:
            if str(m.get("uuid")) == str(recording_uuid):
                meeting = m
                break
        if meeting or not data.get("next_page_token"):
            break
        next_page = data["next_page_token"]

    if not meeting:
        raise HTTPException(404, "Recording not found on Zoom")

    primary = zoom_provider.pick_primary_video_file(meeting)
    if not primary:
        raise HTTPException(400, "This recording has no playable video file.")

    # Dedupe: same meeting uuid imported twice → surface the existing row.
    import_id = str(uuid.uuid4())
    inserted = await db.create_integration_import(
        import_id,
        row["id"],
        user["user_id"],
        external_id=str(recording_uuid),
        external_title=meeting.get("topic"),
        status="queued",
    )
    if not inserted:
        existing = await db.list_integration_imports(row["id"], limit=50)
        for item in existing:
            if item["external_id"] == str(recording_uuid):
                return {
                    "status": "already_imported",
                    "transcription_id": item.get("transcription_id"),
                }
        raise HTTPException(409, "Recording already imported but no record found.")

    # Register a transcription row up-front so the UI can poll progress
    # exactly like a normal upload. The background task fills in the video
    # file and enqueues the transcription worker.
    job_id = str(uuid.uuid4())
    filename = f"{(meeting.get('topic') or 'Zoom recording').strip()}.mp4"[:255]
    await db.create_transcription(job_id, filename, 0, user_id=user["user_id"])
    await db.update_integration_import(import_id, transcription_id=job_id)

    background.add_task(
        _background_zoom_import,
        job_id=job_id,
        import_id=import_id,
        file_meta=primary,
        access_token_snapshot=access_token,
        integration_row_snapshot=row,
    )
    return {"status": "queued", "transcription_id": job_id}


async def _background_zoom_import(
    *,
    job_id: str,
    import_id: str,
    file_meta: dict,
    access_token_snapshot: str,
    integration_row_snapshot: dict,
) -> None:
    """Download the recording to uploads/, then hand off to the existing
    transcription pipeline the same way a user upload would. Runs as a
    FastAPI BackgroundTask so the /import response returns immediately."""
    # Imports kept local so the routes module stays import-cheap at boot.
    from pathlib import Path as _Path
    from app import UPLOAD_DIR, _track_transcription, _run_with_semaphore  # type: ignore

    dest = _Path(UPLOAD_DIR) / f"{job_id}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await db.update_integration_import(import_id, status="downloading")
        try:
            await zoom_provider.download_recording_file(
                file_meta, access_token_snapshot, str(dest),
            )
        except Exception as e:
            # If the snapshot token expired mid-download, try one refresh.
            logger.info("Zoom download retry after: %s", e)
            row = await db.get_integration(integration_row_snapshot["id"], integration_row_snapshot["user_id"])
            if not row:
                raise
            fresh = await _fresh_zoom_access_token(row)
            await zoom_provider.download_recording_file(file_meta, fresh, str(dest))

        size = dest.stat().st_size
        await db.update_transcription(job_id, video_path=str(dest), file_size=size)
        await db.update_integration_import(import_id, status="done")

        import asyncio as _asyncio
        _track_transcription(_asyncio.create_task(_run_with_semaphore(job_id, dest)))
    except Exception as e:
        logger.exception("Zoom import failed for job %s", job_id)
        await db.update_transcription(job_id, status="error",
                                      error_message=f"Zoom import failed: {e}")
        await db.update_integration_import(import_id, status="error",
                                           error_message=str(e)[:500])


# httpx used only by the /zoom/import inline request; exposed here so
# `import httpx` at the module top-level doesn't create a hard dep when
# the integrations surface is dormant (e.g. tests that don't touch Zoom).
import httpx  # noqa: E402
