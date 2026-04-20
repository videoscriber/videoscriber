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
import base64
import json
import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException

import auth
import database as db

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
        # Flipped on once ZOOM_CLIENT_ID is set in the env (Phase 2).
        "configured": bool(os.getenv("ZOOM_CLIENT_ID")),
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
# OAuth providers — Phase 2/3 wiring. For now these endpoints return a
# friendly "not yet configured" 503 when the relevant env vars are missing
# so the Settings UI can show a helpful message instead of a 500.
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
    # Real OAuth handoff lands in Phase 2 (Zoom) and Phase 3 (Google).
    raise HTTPException(501, "OAuth flow not yet implemented for this provider.")
