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

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.requests import Request

import auth
import database as db
import google_provider
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
        "configured": google_provider.is_configured(),
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
    # Default to 'auto' for local folder — it's the expected UX. Unlike
    # cloud providers, there's no plan-gating concern because the file lives
    # on the user's own machine and the upload volume is their own disk.
    await db.upsert_integration(
        integration_id,
        user["user_id"],
        "local_folder",
        account_label=folder_path,
        settings_json=json.dumps({"folder_path": folder_path}),
        sync_mode="auto",
    )
    row = await db.get_integration_by_provider(user["user_id"], "local_folder")
    return _public_integration(row)


@router.post("/local_folder/upload")
async def upload_local_folder_file(
    integration_id: str = Form(...),
    external_id: str = Form(...),
    filename: str = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(auth.require_user),
):
    """Upload a video file discovered by the desktop folder watcher.

    The Electron watcher sends:
      - `integration_id`: which local_folder integration this came from
      - `external_id`: stable dedupe key (the watcher uses `<abs_path>::<size>`)
      - `filename`: display name for the transcription (usually basename)
      - `file`: the MP4/MOV/etc. multipart body

    We dedupe on (integration_id, external_id) so a file seen on every
    poll cycle is only ever uploaded once. On dedupe hit we return the
    existing transcription_id so the watcher can update its local state
    without re-queuing."""
    row = await db.get_integration(integration_id, user["user_id"])
    if not row or row["provider"] != "local_folder":
        raise HTTPException(404, "Local folder integration not found")
    if row.get("sync_mode") == "off":
        raise HTTPException(403, "Folder sync is off — turn it on in Settings.")

    # Dedupe upfront: check if we've already seen this external_id.
    existing = await db.list_integration_imports(integration_id, limit=200)
    prior = next((it for it in existing if it["external_id"] == external_id), None)
    if prior and prior.get("transcription_id"):
        return {"status": "already_imported", "transcription_id": prior["transcription_id"]}

    # Save the uploaded bytes to uploads/ — same path convention as the
    # manual upload route so the rest of the pipeline doesn't care where
    # the file came from.
    from app import UPLOAD_DIR, _track_transcription, _run_with_semaphore  # noqa
    safe_name = _safe_filename_upload(filename) or "recording.mp4"
    job_id = str(uuid.uuid4())
    ext = os.path.splitext(safe_name)[1].lower() or ".mp4"
    dest = UPLOAD_DIR / f"{job_id}{ext}"

    size = 0
    with open(dest, "wb") as fh:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            fh.write(chunk)

    # Register the dedupe + transcription rows. If dedupe inserts fail now
    # (two parallel watchers racing), we roll back the file write.
    import_id = str(uuid.uuid4())
    inserted = await db.create_integration_import(
        import_id,
        integration_id,
        user["user_id"],
        external_id=external_id,
        external_title=safe_name,
        status="downloading",
    )
    if not inserted:
        dest.unlink(missing_ok=True)
        existing = await db.list_integration_imports(integration_id, limit=200)
        prior = next((it for it in existing if it["external_id"] == external_id), None)
        if prior and prior.get("transcription_id"):
            return {"status": "already_imported", "transcription_id": prior["transcription_id"]}
        raise HTTPException(409, "Race with another watcher; try again shortly.")

    await db.create_transcription(job_id, safe_name, size, user_id=user["user_id"])
    await db.update_integration_import(import_id, transcription_id=job_id, status="done")
    await db.update_transcription(job_id, video_path=str(dest))
    await db.update_integration_sync_state(
        integration_id,
        last_sync_at=datetime.now(timezone.utc).isoformat(),
        last_sync_status="ok",
        last_sync_error=None,
    )

    _track_transcription(asyncio.create_task(_run_with_semaphore(job_id, dest)))
    return {"status": "queued", "transcription_id": job_id, "import_id": import_id}


def _safe_filename_upload(name: str) -> str:
    """Reuse the same rules as app._safe_filename without a circular import."""
    from pathlib import Path as _P
    name = _P(name).name
    name = "".join(c for c in name if c.isprintable() and c != '"')
    return name.strip()[:255]


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
    if provider == "google_meet":
        return {"redirect": google_provider.build_authorize_url(user["user_id"])}

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

    result = await _queue_zoom_import(
        user_id=user["user_id"],
        integration_row=row,
        meeting=meeting,
        access_token=access_token,
        background=background,
        allow_reimport=True,  # manual picker — deleted recordings can come back
    )
    if result["status"] == "no_video":
        raise HTTPException(400, "This recording has no playable video file.")
    if result["status"] == "error":
        raise HTTPException(409, result.get("error") or "Recording already imported.")
    return result


async def _queue_zoom_import(
    *,
    user_id: str,
    integration_row: dict,
    meeting: dict,
    access_token: str,
    background: BackgroundTasks | None,
    allow_reimport: bool = False,
) -> dict:
    """Shared queue helper. Picks the primary video file, dedupes against
    integration_imports, creates a transcription stub so the UI can poll,
    and schedules the actual download as a background task.

    `allow_reimport` distinguishes the two dedupe policies:
      - False (default, auto-sync): permanent dedupe by external_id. Once a
        recording has been imported we never touch it again, even if the
        user deleted the transcription — that's the signal we respect.
      - True (manual picker): if the prior transcription was deleted
        (transcription_id is NULL after the cascade in delete_transcription),
        allow a fresh import by appending a reimport suffix to the
        external_id so the unique index still holds.

    Returns a dict describing the outcome:
      {status: 'queued', transcription_id, import_id}
      {status: 'already_imported', transcription_id}
      {status: 'no_video'}
      {status: 'error', error}

    Used by:
      - POST /zoom/import   (manual picker action — allow_reimport=True)
      - POST /zoom/webhook  (recording.completed event)
      - _zoom_auto_sync_loop (background poller)
    """
    primary = zoom_provider.pick_primary_video_file(meeting)
    if not primary:
        return {"status": "no_video"}

    recording_uuid = str(meeting.get("uuid") or "")
    external_id = recording_uuid
    import_id = str(uuid.uuid4())
    inserted = await db.create_integration_import(
        import_id,
        integration_row["id"],
        user_id,
        external_id=external_id,
        external_title=meeting.get("topic"),
        status="queued",
    )
    if not inserted:
        # Look at the prior import(s) for this recording.
        existing = await db.list_integration_imports(integration_row["id"], limit=50)
        prior = next(
            (it for it in existing if it["external_id"].split("#", 1)[0] == recording_uuid),
            None,
        )
        if not prior:
            return {"status": "error", "error": "Recording already seen but no record found."}

        # Live transcription still exists → classic dedupe, same as before.
        if prior.get("transcription_id"):
            return {
                "status": "already_imported",
                "transcription_id": prior["transcription_id"],
            }

        # Prior transcription was deleted. Auto-sync respects the deletion
        # permanently; only a manual re-import gets a fresh record.
        if not allow_reimport:
            return {"status": "already_imported", "transcription_id": None}

        # Manual re-import path: suffix the external_id with a timestamp so
        # the unique (integration_id, external_id) index still holds, and
        # the auto-sync dedupe keeps matching on the bare UUID.
        external_id = f"{recording_uuid}#reimport-{int(datetime.now(timezone.utc).timestamp())}"
        inserted = await db.create_integration_import(
            import_id,
            integration_row["id"],
            user_id,
            external_id=external_id,
            external_title=meeting.get("topic"),
            status="queued",
        )
        if not inserted:
            # Incredibly unlikely — same user hit reimport twice in the same
            # second. Bump the suffix with a random disambiguator.
            external_id = f"{external_id}-{uuid.uuid4().hex[:6]}"
            await db.create_integration_import(
                import_id,
                integration_row["id"],
                user_id,
                external_id=external_id,
                external_title=meeting.get("topic"),
                status="queued",
            )

    job_id = str(uuid.uuid4())
    filename = f"{(meeting.get('topic') or 'Zoom recording').strip()}.mp4"[:255]
    await db.create_transcription(job_id, filename, 0, user_id=user_id)
    await db.update_integration_import(import_id, transcription_id=job_id)

    coro_args = dict(
        job_id=job_id,
        import_id=import_id,
        file_meta=primary,
        access_token_snapshot=access_token,
        integration_row_snapshot=integration_row,
    )
    if background is not None:
        background.add_task(_background_zoom_import, **coro_args)
    else:
        # Webhook + polling paths don't have a BackgroundTasks handle.
        # Fire-and-forget with asyncio; exceptions are caught inside.
        asyncio.create_task(_background_zoom_import(**coro_args))

    return {
        "status": "queued",
        "transcription_id": job_id,
        "import_id": import_id,
    }


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


# ---------------------------------------------------------------------------
# Zoom webhook — real-time auto-sync trigger
# ---------------------------------------------------------------------------
# Exempted from the session middleware in app.py because Zoom calls us
# unauthenticated; we verify the HMAC signature ourselves instead.
@router.post("/zoom/webhook")
async def zoom_webhook(request: Request):
    """Handle `recording.completed` events from Zoom.

    Zoom sends two kinds of requests:
      1. `endpoint.url_validation` — a one-off handshake whenever the
         webhook URL is set or changed. Must be answered with the
         plainToken + HMAC-signed encryptedToken.
      2. `recording.completed` — an actual recording finished processing.
         We look up the matching integration by host email, check that
         sync_mode is 'auto', and queue an import.
    """
    body = await request.body()
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    event = payload.get("event")

    # URL validation: no signature header yet, Zoom proves the handshake
    # by checking *our* response. Do it first and short-circuit.
    if event == "endpoint.url_validation":
        plain = (payload.get("payload") or {}).get("plainToken", "")
        if not plain:
            raise HTTPException(400, "Missing plainToken")
        try:
            return zoom_provider.build_url_validation_response(plain)
        except RuntimeError as e:
            logger.warning("Webhook not configured: %s", e)
            raise HTTPException(503, "Webhook not configured on this server")

    # Non-handshake events: verify Zoom's HMAC signature on the raw body.
    try:
        zoom_provider.verify_webhook_signature(
            body=body,
            signature_header=request.headers.get("x-zm-signature"),
            timestamp_header=request.headers.get("x-zm-request-timestamp"),
        )
    except ValueError as e:
        logger.warning("Zoom webhook signature rejected: %s", e)
        raise HTTPException(401, "Invalid webhook signature")
    except RuntimeError as e:
        logger.warning("Webhook not configured: %s", e)
        raise HTTPException(503, "Webhook not configured on this server")

    if event != "recording.completed":
        # Acknowledge-and-ignore anything else (Zoom sends a bunch of
        # meeting.* events as users enable additional subscriptions).
        return {"ok": True, "ignored": event}

    obj = (payload.get("payload") or {}).get("object") or {}
    host_email = (obj.get("host_email") or "").strip().lower()
    if not host_email:
        logger.info("recording.completed without host_email; skipping")
        return {"ok": True, "skipped": "no_host_email"}

    # Find the user whose Zoom connection matches this host. We stored the
    # Zoom email in account_label at OAuth time, so this is a simple lookup
    # with no extra API call.
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM integrations "
            "WHERE provider=? AND lower(account_label)=? AND sync_mode='auto' "
            "LIMIT 1",
            ("zoom", host_email),
        ) as cur:
            row_raw = await cur.fetchone()
    if not row_raw:
        # Either no matching integration, or sync_mode is off/manual.
        return {"ok": True, "skipped": "no_auto_sync_match"}
    row = dict(row_raw)

    try:
        access_token = await _fresh_zoom_access_token(row)
        await _queue_zoom_import(
            user_id=row["user_id"],
            integration_row=row,
            meeting=obj,
            access_token=access_token,
            background=None,
        )
        await db.update_integration_sync_state(
            row["id"],
            last_sync_at=datetime.now(timezone.utc).isoformat(),
            last_sync_status="ok",
            last_sync_error=None,
        )
    except Exception as e:
        logger.exception("Zoom webhook import failed for %s", row["id"])
        await db.update_integration_sync_state(
            row["id"],
            last_sync_at=datetime.now(timezone.utc).isoformat(),
            last_sync_status="error",
            last_sync_error=str(e)[:500],
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Zoom polling worker — catches recordings webhooks may have missed, and
# backfills for accounts where webhooks aren't configured yet.
# ---------------------------------------------------------------------------
ZOOM_POLL_INTERVAL_S = int(os.getenv("ZOOM_POLL_INTERVAL_S", "1800"))  # 30 min


async def zoom_auto_sync_tick() -> dict:
    """One pass of the Zoom auto-sync poller. Iterates every zoom
    integration with sync_mode='auto', lists recordings newer than
    last_sync_at, and queues imports for anything we haven't seen yet.

    Returns a small stats dict for observability / log lines."""
    import aiosqlite
    stats = {"integrations_checked": 0, "queued": 0, "errors": 0}
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM integrations WHERE provider='zoom' AND sync_mode='auto'"
        ) as cur:
            rows = [dict(r) async for r in cur]

    for row in rows:
        stats["integrations_checked"] += 1
        try:
            access_token = await _fresh_zoom_access_token(row)
            # Window: since last successful sync, back-off 10 min for clock
            # skew. First-ever sync pulls the last 7 days so we don't flood
            # the user with their entire backlog.
            since_iso = row.get("last_sync_at")
            now = datetime.now(timezone.utc)
            if since_iso:
                try:
                    since_dt = datetime.fromisoformat(since_iso) - timedelta(minutes=10)
                except ValueError:
                    since_dt = now - timedelta(days=1)
            else:
                since_dt = now - timedelta(days=7)
            from_date = since_dt.date().isoformat()
            to_date = now.date().isoformat()

            next_page = None
            meetings_seen = 0
            while True:
                data = await zoom_provider.list_recordings(
                    access_token,
                    from_date=from_date,
                    to_date=to_date,
                    page_size=300,
                    next_page_token=next_page,
                )
                for meeting in data.get("meetings") or []:
                    meetings_seen += 1
                    result = await _queue_zoom_import(
                        user_id=row["user_id"],
                        integration_row=row,
                        meeting=meeting,
                        access_token=access_token,
                        background=None,
                    )
                    if result["status"] == "queued":
                        stats["queued"] += 1
                next_page = data.get("next_page_token") or None
                if not next_page:
                    break

            await db.update_integration_sync_state(
                row["id"],
                last_sync_at=now.isoformat(),
                last_sync_status="ok",
                last_sync_error=None,
            )
            logger.info("Zoom auto-sync: integration=%s meetings=%d queued(new)=%s",
                        row["id"], meetings_seen, stats["queued"])
        except Exception as e:
            stats["errors"] += 1
            logger.warning("Zoom auto-sync failed for integration %s: %s", row["id"], e)
            await db.update_integration_sync_state(
                row["id"],
                last_sync_at=datetime.now(timezone.utc).isoformat(),
                last_sync_status="error",
                last_sync_error=str(e)[:500],
            )
    return stats


async def zoom_auto_sync_loop() -> None:
    """Run zoom_auto_sync_tick every ZOOM_POLL_INTERVAL_S until cancelled.
    Intended to be kicked off from app.py's lifespan."""
    while True:
        try:
            await zoom_auto_sync_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Zoom auto-sync loop iteration failed: %s", e)
        try:
            await asyncio.sleep(ZOOM_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise


# ---------------------------------------------------------------------------
# Google Meet integration — same OAuth → picker → import → poll pattern as
# Zoom, using Drive as the recording store. Webhooks would use Drive Changes
# push notifications (more setup overhead) — we stick with polling for now;
# real-time can be layered on later.
# ---------------------------------------------------------------------------
GOOGLE_POLL_INTERVAL_S = int(os.getenv("GOOGLE_POLL_INTERVAL_S", "1800"))


def _google_expiry(expires_in: int | None) -> str:
    seconds = max(60, int(expires_in or 3600) - 60)
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


async def _save_google_tokens(
    integration_id: str,
    user_id: str,
    account_label: str | None,
    token_bundle: dict,
) -> None:
    await db.upsert_integration(
        integration_id,
        user_id,
        "google_meet",
        account_label=account_label,
        access_token_encrypted=_encrypt(token_bundle.get("access_token")),
        refresh_token_encrypted=_encrypt(token_bundle.get("refresh_token")),
        token_expires_at=_google_expiry(token_bundle.get("expires_in")),
        sync_mode="manual",
    )


async def _fresh_google_access_token(row: dict) -> str:
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
        raise HTTPException(400, "Google connection is missing a refresh token — reconnect.")
    try:
        bundle = await google_provider.refresh_access_token(refresh_token)
    except Exception as e:
        logger.warning("Google token refresh failed for integration %s: %s", row["id"], e)
        raise HTTPException(401, "Google token refresh failed. Disconnect and reconnect.")

    await _save_google_tokens(row["id"], row["user_id"], row.get("account_label"), bundle)
    return bundle["access_token"]


@router.get("/google_meet/oauth/callback")
async def google_oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    """Google redirects here after the consent screen. State-verifies the
    user, exchanges the code, labels the integration with the Google
    account's email, and bounces back to Settings with a status flag."""
    if error:
        logger.info("Google authorization declined: %s", error)
        return RedirectResponse(
            "/app/settings?integration=google_meet&status=cancelled",
            status_code=303,
        )
    if not code or not state:
        raise HTTPException(400, "Missing code or state from Google")

    try:
        user_id = google_provider.parse_state(state)
    except ValueError as e:
        logger.warning("Google OAuth state rejected: %s", e)
        raise HTTPException(400, "Invalid or expired authorization state.")

    try:
        bundle = await google_provider.exchange_code(code)
    except RuntimeError as e:
        logger.warning("Google code exchange failed: %s", e)
        return RedirectResponse(
            "/app/settings?integration=google_meet&status=error",
            status_code=303,
        )

    access_token = bundle.get("access_token")
    if not access_token:
        raise HTTPException(502, "Google returned no access token")

    try:
        userinfo = await google_provider.fetch_userinfo(access_token)
    except Exception as e:
        logger.warning("Google userinfo failed: %s", e)
        userinfo = {}
    account_label = userinfo.get("email") or userinfo.get("name") or "Google"

    existing = await db.get_integration_by_provider(user_id, "google_meet")
    integration_id = existing["id"] if existing else str(uuid.uuid4())
    await _save_google_tokens(integration_id, user_id, account_label, bundle)
    logger.info("Google Meet connected for user=%s, account=%s", user_id, account_label)

    return RedirectResponse(
        "/app/settings?integration=google_meet&status=connected",
        status_code=303,
    )


@router.get("/google_meet/recordings")
async def list_google_meet_recordings(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    page_token: str | None = Query(default=None),
    user: dict = Depends(auth.require_user),
):
    """List Meet recordings (MP4 files in the user's Drive 'Meet Recordings'
    folder) matching the supplied date window."""
    row = await db.get_integration_by_provider(user["user_id"], "google_meet")
    if not row:
        raise HTTPException(404, "Google Meet is not connected for this account.")
    access_token = await _fresh_google_access_token(row)
    try:
        data = await google_provider.list_meet_recordings(
            access_token,
            from_date=from_date,
            to_date=to_date,
            page_token=page_token,
        )
    except httpx.HTTPStatusError as e:
        logger.warning(
            "Google Drive list failed: %s %s", e.response.status_code, e.response.text[:500]
        )
        raise HTTPException(502, "Google Drive API error")

    # Shape for the picker. We reuse Zoom's picker payload shape so the
    # frontend treats both providers uniformly.
    items = []
    for f in data.get("files") or []:
        duration_ms = (f.get("videoMediaMetadata") or {}).get("durationMillis")
        duration_min = None
        if duration_ms:
            try:
                duration_min = round(int(duration_ms) / 60000)
            except (TypeError, ValueError):
                duration_min = None
        items.append({
            "uuid": f.get("id"),  # Drive file id; picker treats as opaque
            "topic": f.get("name") or "Meet recording",
            "start_time": f.get("createdTime"),
            "duration_minutes": duration_min,
            "total_size": f.get("size"),
            "has_video": True,
            "file_id": f.get("id"),
        })
    return {
        "items": items,
        "next_page_token": data.get("nextPageToken"),
    }


@router.post("/google_meet/import")
async def import_google_meet_recording(
    background: BackgroundTasks,
    recording_uuid: str = Form(...),  # Drive file id
    user: dict = Depends(auth.require_user),
):
    """Queue an import of a Meet recording by Drive file id. Background
    task downloads the MP4 and hands it to the transcription pipeline."""
    row = await db.get_integration_by_provider(user["user_id"], "google_meet")
    if not row:
        raise HTTPException(404, "Google Meet is not connected for this account.")
    access_token = await _fresh_google_access_token(row)

    # Fetch the file metadata so we have a fresh name + size for the
    # transcription row's filename.
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{google_provider.DRIVE_API_BASE}/files/{recording_uuid}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "id,name,size,createdTime", "supportsAllDrives": "true"},
        )
    if resp.status_code == 404:
        raise HTTPException(404, "Recording not found on Google Drive")
    if resp.status_code != 200:
        logger.warning("Google Drive fetch failed: %s %s", resp.status_code, resp.text[:500])
        raise HTTPException(502, "Google Drive API error")
    meta = resp.json()

    result = await _queue_google_import(
        user_id=user["user_id"],
        integration_row=row,
        file_meta=meta,
        access_token=access_token,
        background=background,
        allow_reimport=True,
    )
    if result["status"] == "error":
        raise HTTPException(409, result.get("error") or "Recording already imported.")
    return result


async def _queue_google_import(
    *,
    user_id: str,
    integration_row: dict,
    file_meta: dict,
    access_token: str,
    background: BackgroundTasks | None,
    allow_reimport: bool = False,
) -> dict:
    """Mirror of _queue_zoom_import for Google Meet. Dedupes by Drive file
    id. Same reimport semantics: auto-sync respects deletions permanently;
    manual picker can re-import by suffixing the external_id."""
    file_id = str(file_meta.get("id") or "")
    if not file_id:
        return {"status": "no_video"}

    external_id = file_id
    import_id = str(uuid.uuid4())
    inserted = await db.create_integration_import(
        import_id,
        integration_row["id"],
        user_id,
        external_id=external_id,
        external_title=file_meta.get("name"),
        status="queued",
    )
    if not inserted:
        existing = await db.list_integration_imports(integration_row["id"], limit=50)
        prior = next(
            (it for it in existing if it["external_id"].split("#", 1)[0] == file_id),
            None,
        )
        if not prior:
            return {"status": "error", "error": "Recording already seen but no record found."}
        if prior.get("transcription_id"):
            return {"status": "already_imported", "transcription_id": prior["transcription_id"]}
        if not allow_reimport:
            return {"status": "already_imported", "transcription_id": None}
        external_id = f"{file_id}#reimport-{int(datetime.now(timezone.utc).timestamp())}"
        inserted = await db.create_integration_import(
            import_id,
            integration_row["id"],
            user_id,
            external_id=external_id,
            external_title=file_meta.get("name"),
            status="queued",
        )
        if not inserted:
            external_id = f"{external_id}-{uuid.uuid4().hex[:6]}"
            await db.create_integration_import(
                import_id,
                integration_row["id"],
                user_id,
                external_id=external_id,
                external_title=file_meta.get("name"),
                status="queued",
            )

    job_id = str(uuid.uuid4())
    filename = f"{(file_meta.get('name') or 'Meet recording').strip()}"
    if not filename.lower().endswith(".mp4"):
        filename = f"{filename}.mp4"
    filename = filename[:255]
    await db.create_transcription(job_id, filename, 0, user_id=user_id)
    await db.update_integration_import(import_id, transcription_id=job_id)

    coro_args = dict(
        job_id=job_id,
        import_id=import_id,
        file_id=file_id,
        access_token_snapshot=access_token,
        integration_row_snapshot=integration_row,
    )
    if background is not None:
        background.add_task(_background_google_import, **coro_args)
    else:
        asyncio.create_task(_background_google_import(**coro_args))

    return {"status": "queued", "transcription_id": job_id, "import_id": import_id}


async def _background_google_import(
    *,
    job_id: str,
    import_id: str,
    file_id: str,
    access_token_snapshot: str,
    integration_row_snapshot: dict,
) -> None:
    from pathlib import Path as _Path
    from app import UPLOAD_DIR, _track_transcription, _run_with_semaphore  # type: ignore

    dest = _Path(UPLOAD_DIR) / f"{job_id}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await db.update_integration_import(import_id, status="downloading")
        try:
            await google_provider.download_drive_file(file_id, access_token_snapshot, str(dest))
        except Exception as e:
            logger.info("Google download retry after: %s", e)
            row = await db.get_integration(
                integration_row_snapshot["id"], integration_row_snapshot["user_id"]
            )
            if not row:
                raise
            fresh = await _fresh_google_access_token(row)
            await google_provider.download_drive_file(file_id, fresh, str(dest))

        size = dest.stat().st_size
        await db.update_transcription(job_id, video_path=str(dest), file_size=size)
        await db.update_integration_import(import_id, status="done")

        import asyncio as _asyncio
        _track_transcription(_asyncio.create_task(_run_with_semaphore(job_id, dest)))
    except Exception as e:
        logger.exception("Google Meet import failed for job %s", job_id)
        await db.update_transcription(
            job_id, status="error", error_message=f"Google Meet import failed: {e}"
        )
        await db.update_integration_import(import_id, status="error", error_message=str(e)[:500])


async def google_auto_sync_tick() -> dict:
    """Poll every Google Meet integration with sync_mode='auto' for new
    recordings since last_sync_at. Mirror of zoom_auto_sync_tick."""
    import aiosqlite
    stats = {"integrations_checked": 0, "queued": 0, "errors": 0}
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM integrations WHERE provider='google_meet' AND sync_mode='auto'"
        ) as cur:
            rows = [dict(r) async for r in cur]

    for row in rows:
        stats["integrations_checked"] += 1
        try:
            access_token = await _fresh_google_access_token(row)
            since_iso = row.get("last_sync_at")
            now = datetime.now(timezone.utc)
            if since_iso:
                try:
                    since_dt = datetime.fromisoformat(since_iso) - timedelta(minutes=10)
                except ValueError:
                    since_dt = now - timedelta(days=1)
            else:
                since_dt = now - timedelta(days=7)
            from_date = since_dt.date().isoformat()
            to_date = now.date().isoformat()

            page_token = None
            files_seen = 0
            while True:
                data = await google_provider.list_meet_recordings(
                    access_token,
                    from_date=from_date,
                    to_date=to_date,
                    page_size=100,
                    page_token=page_token,
                )
                for f in data.get("files") or []:
                    files_seen += 1
                    r = await _queue_google_import(
                        user_id=row["user_id"],
                        integration_row=row,
                        file_meta=f,
                        access_token=access_token,
                        background=None,
                    )
                    if r["status"] == "queued":
                        stats["queued"] += 1
                page_token = data.get("nextPageToken")
                if not page_token:
                    break

            await db.update_integration_sync_state(
                row["id"],
                last_sync_at=now.isoformat(),
                last_sync_status="ok",
                last_sync_error=None,
            )
            logger.info(
                "Google auto-sync: integration=%s files=%d queued(new)=%s",
                row["id"], files_seen, stats["queued"],
            )
        except Exception as e:
            stats["errors"] += 1
            logger.warning("Google auto-sync failed for integration %s: %s", row["id"], e)
            await db.update_integration_sync_state(
                row["id"],
                last_sync_at=datetime.now(timezone.utc).isoformat(),
                last_sync_status="error",
                last_sync_error=str(e)[:500],
            )
    return stats


async def google_auto_sync_loop() -> None:
    while True:
        try:
            await google_auto_sync_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Google auto-sync loop iteration failed: %s", e)
        try:
            await asyncio.sleep(GOOGLE_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise


# httpx lives here for the /zoom/recordings error-handling reference; kept at
# the bottom so the lazy-import comment from Phase 2 still reads naturally.
import httpx  # noqa: E402
