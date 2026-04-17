"""Thin wrapper around the Resend Domains API.

Resend's Python SDK exposes synchronous HTTP calls; we run them in the default
asyncio threadpool so FastAPI handlers can `await` them without blocking the
event loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import resend

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY")

_configured = False
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]+(\.[a-z0-9-]+)+$")


def _ensure_configured() -> None:
    global _configured
    if _configured:
        return
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY is not set")
    resend.api_key = RESEND_API_KEY
    _configured = True


def normalize_domain(raw: str) -> str:
    """Lowercase + strip protocol/path. Raises ValueError on bad input."""
    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/", 1)[0]
    if not _DOMAIN_RE.match(d):
        raise ValueError("Please enter a valid domain like example.com")
    return d


async def create_domain(domain: str) -> dict:
    """Create a domain in Resend. Returns {id, name, status, records[]}."""
    _ensure_configured()
    result = await asyncio.to_thread(resend.Domains.create, {"name": domain})
    return _normalize(result)


async def get_domain(domain_id: str) -> dict:
    _ensure_configured()
    result = await asyncio.to_thread(resend.Domains.get, domain_id)
    return _normalize(result)


async def verify_domain(domain_id: str) -> dict:
    """Trigger verification. Resend's verify returns the updated Domain, but
    its records may be stale, so we re-fetch to surface fresh status + records."""
    _ensure_configured()
    await asyncio.to_thread(resend.Domains.verify, domain_id)
    return await get_domain(domain_id)


async def delete_domain(domain_id: str) -> None:
    _ensure_configured()
    try:
        await asyncio.to_thread(resend.Domains.remove, domain_id)
    except Exception as e:
        # Deleting a stale record is not fatal — we still want to clear our side.
        logger.warning("Resend domain delete failed for %s: %s", domain_id, e)


def _normalize(result) -> dict:
    """Flatten Resend's response (a TypedDict, i.e. a dict at runtime) into a
    stable shape our frontend can render without caring about SDK specifics."""
    raw = dict(result) if isinstance(result, dict) else {}
    records = raw.get("records") or []
    simplified = []
    for r in records:
        if not isinstance(r, dict):
            continue
        simplified.append({
            "type": (r.get("type") or "").upper(),
            "name": r.get("name") or "",
            "value": r.get("value") or "",
            "ttl": r.get("ttl") or "Auto",
            "priority": r.get("priority"),
            "status": r.get("status") or "",
        })
    return {
        "id": raw.get("id") or "",
        "name": raw.get("name") or "",
        "status": (raw.get("status") or "pending").lower(),
        "records": simplified,
    }
