import asyncio
import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import uvicorn
from dotenv import load_dotenv

# Load .env BEFORE importing our own modules — several of them read env vars
# at import time (e.g. auth_routes.AUTH_MODE, sms/email service flags).
load_dotenv()

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from starlette.requests import Request  # noqa: E402

import auth  # noqa: E402
import auth_routes  # noqa: E402
import billing_routes  # noqa: E402
import chat_routes  # noqa: E402
import database as db  # noqa: E402
import domain_routes  # noqa: E402
from transcriber import process_transcription  # noqa: E402

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1000"))
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
DESKTOP_MODE = os.getenv("VIDEOSCRIBER_DESKTOP") == "1"
USER_ENV_PATH = os.getenv("VIDEOSCRIBER_USER_ENV_PATH") or ""

UPLOAD_DIR = Path("uploads")
AUDIO_DIR = Path("audio")

ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".m4a", ".mp3", ".wav", ".flac", ".ogg",
}
VIDEO_PREVIEW_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

MAX_FILENAME_LEN = 255
MAX_EMAIL_SUBJECT_LEN = 200
MAX_EMAIL_BODY_LEN = 100_000

# Freemium plan limits (cloud only; desktop is unmetered)
FREE_MAX_FILE_MB = int(os.getenv("FREE_MAX_FILE_MB", "250"))
FREE_MONTHLY_LIMIT = int(os.getenv("FREE_MONTHLY_LIMIT", "3"))
PLUS_MAX_FILE_MB = int(os.getenv("PLUS_MAX_FILE_MB", "1024"))
USAGE_WINDOW_DAYS = 30
# Free-tier transcripts are deleted after this many days
FREE_RETENTION_DAYS = int(os.getenv("FREE_RETENTION_DAYS", "10"))


def _plan_limits(plan: str) -> dict:
    if plan == "plus":
        return {"max_file_mb": PLUS_MAX_FILE_MB, "monthly_limit": None}
    return {"max_file_mb": FREE_MAX_FILE_MB, "monthly_limit": FREE_MONTHLY_LIMIT}


async def _usage_count(user_id: str) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=USAGE_WINDOW_DAYS)).isoformat()
    return await db.count_transcriptions_since(user_id, since)


def _require_owner(record: dict | None, user: dict) -> dict:
    """404 if record is missing or doesn't belong to the user."""
    if not record or record.get("user_id") != user["user_id"]:
        raise HTTPException(404, "Transcription not found")
    return record


# Track in-flight transcription tasks so graceful shutdown can wait on them.
_inflight_transcriptions: set[asyncio.Task] = set()


def _track_transcription(task: asyncio.Task) -> None:
    _inflight_transcriptions.add(task)
    task.add_done_callback(_inflight_transcriptions.discard)


async def _sweep_free_tier_retention() -> int:
    """Delete free-plan transcriptions older than FREE_RETENTION_DAYS.
    Also cleans up stored video files. Returns the number deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=FREE_RETENTION_DAYS)).isoformat()
    stale = await db.find_free_transcriptions_older_than(cutoff)
    if not stale:
        return 0
    for row in stale:
        vp = row.get("video_path")
        if vp:
            try:
                Path(vp).unlink(missing_ok=True)
            except Exception as e:
                logger.warning("Retention: failed to unlink %s: %s", vp, e)
    await db.delete_transcriptions_bulk([row["id"] for row in stale])
    logger.info("Retention sweep: deleted %d free-tier transcriptions older than %d days",
                len(stale), FREE_RETENTION_DAYS)
    return len(stale)


async def _retention_loop() -> None:
    """Run the retention sweep hourly while the app is alive."""
    while True:
        try:
            await _sweep_free_tier_retention()
        except Exception as e:
            logger.warning("Retention sweep failed: %s", e)
        await asyncio.sleep(3600)

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _validate_email(addr: str) -> str:
    addr = addr.strip()
    if not addr or len(addr) > 254 or not _EMAIL_RE.match(addr):
        raise HTTPException(400, "Invalid email address")
    return addr


def _reject_crlf(value: str, field: str) -> str:
    if "\r" in value or "\n" in value:
        raise HTTPException(400, f"Invalid characters in {field}")
    return value


def _safe_filename(name: str) -> str:
    """Strip path components and control chars; cap length. May return empty string."""
    name = Path(name).name  # drop any directory components
    # Drop control chars and double-quotes (which would break Content-Disposition)
    name = "".join(c for c in name if c.isprintable() and c != '"')
    return name.strip()[:MAX_FILENAME_LEN]

semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not OPENAI_API_KEY:
        if DESKTOP_MODE:
            logger.warning(
                "OPENAI_API_KEY not set — transcription features will be unavailable "
                "until you add keys via Settings."
            )
        else:
            raise RuntimeError("OPENAI_API_KEY environment variable is required")
    if not shutil.which("ffmpeg"):
        if DESKTOP_MODE:
            logger.warning("ffmpeg not found on PATH — transcription will fail until installed.")
        else:
            raise RuntimeError("ffmpeg is required but not found on PATH")

    UPLOAD_DIR.mkdir(exist_ok=True)
    AUDIO_DIR.mkdir(exist_ok=True)

    await db.init_db()
    if DESKTOP_MODE:
        await auth.ensure_desktop_user()
    await _cleanup_orphans()
    # Run retention sweep immediately, then hourly in background
    try:
        await _sweep_free_tier_retention()
    except Exception as e:
        logger.warning("Initial retention sweep failed: %s", e)
    retention_task = asyncio.create_task(_retention_loop())
    logger.info("Videoscriber ready at http://%s:%s", HOST, PORT)
    try:
        yield
    finally:
        # Graceful shutdown: give in-flight transcriptions up to GRACEFUL_SHUTDOWN_S
        # to finish (so ffmpeg finalises its moov atom and atomic renames land).
        # Anything still running after that is cancelled; startup recovery + the
        # atomic-write rename in enhance_video handle the leftovers safely.
        graceful_s = int(os.getenv("GRACEFUL_SHUTDOWN_S", "60"))
        if _inflight_transcriptions and graceful_s > 0:
            pending = list(_inflight_transcriptions)
            logger.info("Waiting up to %ds for %d in-flight transcription(s)...",
                        graceful_s, len(pending))
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=graceful_s,
                )
            except asyncio.TimeoutError:
                logger.warning("Graceful deadline hit; cancelling %d job(s)",
                               len(_inflight_transcriptions))
                for t in list(_inflight_transcriptions):
                    t.cancel()
                await asyncio.gather(*_inflight_transcriptions, return_exceptions=True)
        retention_task.cancel()


def _upload_job_id(path: Path) -> str:
    """Derive the job_id prefix from an upload filename.

    Upload files follow three naming conventions, all rooted at a UUID job id:
      - {job_id}{ext}           — original uploaded video
      - {job_id}_preview{ext}   — preview-video uploaded separately
      - {job_id}_enhanced.mp4   — output of ffmpeg post-processing
    """
    stem = path.stem
    if stem.endswith("_preview"):
        return stem[: -len("_preview")]
    if stem.endswith("_enhanced"):
        return stem[: -len("_enhanced")]
    return stem


async def _cleanup_orphans() -> None:
    """On startup, prune stale files in uploads/ and audio/.

    Safe to run because init_db() has already flipped any in-flight jobs
    (status in pending/extracting/transcribing) to 'error'. That means no
    active worker owns files in these directories — anything still on disk
    is either tied to a persisted row, or is a leak from a previous crash.

    Audio: *.mp3 is purely a transcription scratchpad. Nothing references
    these across a restart, so every file is removable.

    Uploads: keep files whose job_id prefix matches a live DB row. Also
    re-associate orphaned originals with rows that were interrupted but
    have no video_path set, so retry remains possible.
    """
    records = await db.list_all_transcriptions_for_cleanup()
    by_id = {r["id"]: r for r in records}

    audio_removed = 0
    for p in AUDIO_DIR.glob("*.mp3"):
        try:
            p.unlink()
            audio_removed += 1
        except OSError as e:
            logger.warning("Could not remove stale audio %s: %s", p, e)

    upload_removed = 0
    repaired = 0
    for p in UPLOAD_DIR.iterdir():
        if not p.is_file():
            continue
        job_id = _upload_job_id(p)
        record = by_id.get(job_id)
        if record is None:
            try:
                p.unlink()
                upload_removed += 1
            except OSError as e:
                logger.warning("Could not remove orphan upload %s: %s", p, e)
            continue
        # Re-associate: row is in 'error' with no video_path but the original
        # upload still exists on disk. Reattach so retry can find it.
        if (
            record.get("status") == "error"
            and not record.get("video_path")
            and not p.name.endswith(("_preview" + p.suffix, "_enhanced.mp4"))
        ):
            await db.update_transcription(job_id, video_path=str(p))
            repaired += 1

    if audio_removed or upload_removed or repaired:
        logger.info(
            "Startup cleanup: removed %d audio file(s), %d orphan upload(s); reattached %d interrupted job(s)",
            audio_removed, upload_removed, repaired,
        )


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
# Expose desktop-mode flag to every template so layouts can clear macOS
# traffic-light overlap and mark header regions as draggable.
templates.env.globals["desktop_mode"] = DESKTOP_MODE
app.include_router(auth_routes.router)
app.include_router(billing_routes.router)
app.include_router(chat_routes.router)
app.include_router(domain_routes.router)


@app.middleware("http")
async def gate_api_behind_session(request: Request, call_next):
    """Require a valid session for /api/* routes. /api/sync/* uses its own key auth;
    /api/billing/webhook is Stripe → us and authenticates via signature verification."""
    path = request.url.path
    if (
        path.startswith("/api/")
        and not path.startswith("/api/sync")
        and path != "/api/billing/webhook"
    ):
        user = await auth.current_user(request)
        if not user:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    if DESKTOP_MODE:
        # Single-user local app — skip the marketing landing. Route to key-setup
        # on first launch, otherwise straight into the app.
        if not OPENAI_API_KEY or not os.getenv("ASSEMBLYAI_API_KEY"):
            return RedirectResponse("/setup", status_code=303)
        return RedirectResponse("/app", status_code=303)
    user = await auth.current_user(request)
    if user:
        return RedirectResponse("/app", status_code=303)
    return templates.TemplateResponse(request, "landing.html")


@app.get("/app", response_class=HTMLResponse)
async def app_home(request: Request):
    user = await auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not user.get("profile_completed_at"):
        return RedirectResponse("/signup/profile", status_code=303)
    plan = user.get("plan") or "free"
    limits = _plan_limits(plan)
    used = await _usage_count(user["user_id"])
    return templates.TemplateResponse(request, "index.html", {
        "user": user,
        "plan": plan,
        "plan_limits": limits,
        "plan_used": used,
    })


@app.get("/app/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = await auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not user.get("profile_completed_at"):
        return RedirectResponse("/signup/profile", status_code=303)
    plan = user.get("plan") or "free"
    limits = _plan_limits(plan)
    used = await _usage_count(user["user_id"])
    return templates.TemplateResponse(request, "settings.html", {
        "user": user,
        "plan": plan,
        "plan_limits": limits,
        "plan_used": used,
    })


@app.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request):
    user = await auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    plan = user.get("plan") or "free"
    limits = _plan_limits(plan)
    status_flag = request.query_params.get("status") or ""
    billing_configured = bool(
        os.getenv("STRIPE_SECRET_KEY")
        and os.getenv("STRIPE_PRICE_MONTHLY")
        and os.getenv("STRIPE_PRICE_ANNUAL")
    )
    return templates.TemplateResponse(request, "upgrade.html", {
        "user": user,
        "plan": plan,
        "limits": limits,
        "free_max_file_mb": FREE_MAX_FILE_MB,
        "plus_max_file_mb": PLUS_MAX_FILE_MB,
        "free_monthly_limit": FREE_MONTHLY_LIMIT,
        "billing_configured": billing_configured,
        "status_flag": status_flag,
    })


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return templates.TemplateResponse(request, "legal/terms.html")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return templates.TemplateResponse(request, "legal/privacy.html")


@app.get("/brand", response_class=HTMLResponse)
async def brand_page(request: Request):
    return templates.TemplateResponse(request, "brand.html")


_BRAND_DIR = Path("static/brand")
_BRAND_KITS: dict[str, list[str]] = {
    "logo": ["logo.svg", "logo-600.png", "logo-1200.png", "logo-2400.png"],
    "logo-mono-black": [
        "logo-mono-black.svg", "logo-mono-black-600.png",
        "logo-mono-black-1200.png", "logo-mono-black-2400.png",
    ],
    "logo-mono-white": [
        "logo-mono-white.svg", "logo-mono-white-600.png",
        "logo-mono-white-1200.png", "logo-mono-white-2400.png",
    ],
    "mark": ["mark.svg", "mark-256.png", "mark-512.png", "mark-1024.png", "mark-2048.png"],
    "mark-mono-black": [
        "mark-mono-black.svg", "mark-mono-black-256.png",
        "mark-mono-black-512.png", "mark-mono-black-1024.png", "mark-mono-black-2048.png",
    ],
    "mark-mono-white": [
        "mark-mono-white.svg", "mark-mono-white-256.png",
        "mark-mono-white-512.png", "mark-mono-white-1024.png", "mark-mono-white-2048.png",
    ],
    "wordmark": ["wordmark.svg", "wordmark-600.png", "wordmark-1200.png", "wordmark-2400.png"],
    "wordmark-mono-black": [
        "wordmark-mono-black.svg", "wordmark-mono-black-600.png",
        "wordmark-mono-black-1200.png", "wordmark-mono-black-2400.png",
    ],
    "wordmark-mono-white": [
        "wordmark-mono-white.svg", "wordmark-mono-white-600.png",
        "wordmark-mono-white-1200.png", "wordmark-mono-white-2400.png",
    ],
    "avatar": ["avatar.svg", "avatar-256.png", "avatar-512.png", "avatar-1024.png", "avatar-2048.png"],
}


@app.get("/brand/kit/{slug}.zip")
async def brand_kit_zip(slug: str):
    import io
    import zipfile
    files = _BRAND_KITS.get(slug)
    if not files:
        raise HTTPException(404, "Unknown brand kit")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            path = _BRAND_DIR / name
            if path.exists():
                zf.write(path, name)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="videoscriber-{slug}.zip"',
        },
    )


_release_cache: dict = {"data": None, "fetched_at": 0.0}
_RELEASE_CACHE_TTL = 300  # 5 minutes
_RELEASE_URL = "https://api.github.com/repos/videoscriber/videoscriber/releases/latest"


def _fetch_latest_release_sync() -> dict | None:
    now = time.time()
    if _release_cache["data"] and (now - _release_cache["fetched_at"]) < _RELEASE_CACHE_TTL:
        return _release_cache["data"]
    try:
        req = urllib.request.Request(
            _RELEASE_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "videoscriber-landing",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return _release_cache["data"]
    dmg_assets = [
        {"name": a.get("name"), "url": a.get("browser_download_url"), "size": a.get("size", 0)}
        for a in data.get("assets") or []
        if (a.get("name") or "").endswith(".dmg")
    ]
    result = {
        "tag": data.get("tag_name"),
        "published_at": data.get("published_at"),
        "html_url": data.get("html_url"),
        "dmg_assets": dmg_assets,
    }
    _release_cache["data"] = result
    _release_cache["fetched_at"] = now
    return result


@app.get("/download", response_class=HTMLResponse)
async def download_page(request: Request):
    release = await asyncio.to_thread(_fetch_latest_release_sync)
    return templates.TemplateResponse(
        request,
        "download.html",
        {"release": release},
    )


# ---------------------------------------------------------------- desktop setup
def _desktop_only() -> None:
    if not DESKTOP_MODE:
        raise HTTPException(404, "Not available in this mode")


@app.get("/setup", response_class=HTMLResponse)
async def desktop_setup_page(request: Request):
    _desktop_only()
    keys_configured = bool(OPENAI_API_KEY) and bool(os.getenv("ASSEMBLYAI_API_KEY"))
    return templates.TemplateResponse(
        request,
        "setup.html",
        {"keys_configured": keys_configured},
    )


@app.post("/api/desktop/save-keys")
async def save_desktop_keys(openai_key: str = Form(""), assemblyai_key: str = Form("")):
    _desktop_only()
    if not USER_ENV_PATH:
        raise HTTPException(500, "VIDEOSCRIBER_USER_ENV_PATH not set")
    # Read existing .env if present, merge + rewrite atomically.
    existing: dict[str, str] = {}
    env_path = Path(USER_ENV_PATH)
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v
    if openai_key:
        existing["OPENAI_API_KEY"] = openai_key.strip()
    if assemblyai_key:
        existing["ASSEMBLYAI_API_KEY"] = assemblyai_key.strip()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = env_path.with_suffix(".env.tmp")
    tmp.write_text("".join(f"{k}={v}\n" for k, v in existing.items()))
    os.chmod(tmp, 0o600)
    tmp.replace(env_path)
    return JSONResponse({"ok": True, "restart_required": True})


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/robots.txt")
async def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /app\n"
        "Disallow: /app/\n"
        "Disallow: /api/\n"
        "Disallow: /auth/\n"
        "Disallow: /signup/profile\n"
        "Sitemap: https://videoscriber.ai/sitemap.xml\n"
    )
    return PlainTextResponse(body, media_type="text/plain")


@app.get("/sitemap.xml")
async def sitemap_xml():
    urls = ["/", "/signup", "/login", "/terms", "/privacy", "/upgrade"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entries = "".join(
        f"  <url><loc>https://videoscriber.ai{u}</loc><lastmod>{now}</lastmod>"
        f"<changefreq>{'weekly' if u == '/' else 'monthly'}</changefreq>"
        f"<priority>{'1.0' if u == '/' else '0.6'}</priority></url>\n"
        for u in urls
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}"
        "</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")


@app.get("/api/config")
async def get_config(user: dict = Depends(auth.require_user)):
    plan = user.get("plan") or "free"
    limits = _plan_limits(plan)
    used = await _usage_count(user["user_id"])
    return {
        "diarization_available": bool(os.getenv("ASSEMBLYAI_API_KEY")),
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
        "user": {
            "id": user["user_id"],
            "full_name": user.get("full_name"),
            "email": user.get("email"),
            "phone": user.get("phone"),
        },
        "plan": {
            "tier": plan,
            "max_file_mb": limits["max_file_mb"],
            "monthly_limit": limits["monthly_limit"],
            "used_this_month": used,
            "remaining": None if limits["monthly_limit"] is None else max(0, limits["monthly_limit"] - used),
            "window_days": USAGE_WINDOW_DAYS,
        },
    }


@app.get("/api/queue")
async def get_queue():
    stats = await db.queue_stats()
    return {
        "max_concurrent": MAX_CONCURRENT_JOBS,
        "running": stats["running"],
        "pending": stats["pending"],
    }


@app.post("/api/upload")
async def upload_video(
    files: list[UploadFile],
    diarize: str = Form(default="false"),
    user: dict = Depends(auth.require_user),
):
    use_diarize = diarize.lower() == "true"
    plan = user.get("plan") or "free"
    limits = _plan_limits(plan)
    max_bytes = limits["max_file_mb"] * 1024 * 1024
    window_since_iso = (datetime.now(timezone.utc) - timedelta(days=USAGE_WINDOW_DAYS)).isoformat()

    results = []
    for file in files:
        if not file.filename:
            continue
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(400, f"File type {ext} not supported. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}")

        job_id = str(uuid.uuid4())
        save_path = UPLOAD_DIR / f"{job_id}{ext}"

        size = 0
        with open(save_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    save_path.unlink(missing_ok=True)
                    raise HTTPException(
                        413,
                        f"File too large. Your {plan} plan allows up to {limits['max_file_mb']}MB per file."
                        + (" Upgrade for larger files." if plan == "free" else ""),
                    )
                f.write(chunk)

        # Atomic limit check + insert — serializes across parallel uploads
        reserved = await db.try_create_transcription_atomic(
            job_id,
            user["user_id"],
            file.filename,
            size,
            limits["monthly_limit"],
            window_since_iso if limits["monthly_limit"] is not None else None,
        )
        if not reserved:
            save_path.unlink(missing_ok=True)
            raise HTTPException(
                402,
                f"Free plan allows {limits['monthly_limit']} transcriptions per {USAGE_WINDOW_DAYS} days. "
                f"Upgrade to remove the limit.",
            )

        _track_transcription(asyncio.create_task(_run_with_semaphore(job_id, save_path, use_diarize)))
        results.append({"id": job_id, "status": "pending"})

    return results


async def _run_with_semaphore(job_id: str, video_path: Path, diarize: bool = False):
    async with semaphore:
        await process_transcription(job_id, video_path, AUDIO_DIR, diarize=diarize)


@app.get("/api/transcriptions")
async def list_transcriptions(user: dict = Depends(auth.require_user)):
    return await db.list_transcriptions(user["user_id"])


@app.get("/api/transcriptions/{job_id}")
async def get_transcription(job_id: str, user: dict = Depends(auth.require_user)):
    record = _require_owner(await db.get_transcription(job_id), user)
    return record


@app.get("/api/transcriptions/{job_id}/download/{fmt}")
async def download_transcription(job_id: str, fmt: str, user: dict = Depends(auth.require_user)):
    if fmt not in ("txt", "srt", "vtt", "pdf"):
        raise HTTPException(400, "Format must be txt, srt, vtt, or pdf")

    record = _require_owner(await db.get_transcription(job_id), user)
    if record["status"] != "done":
        raise HTTPException(400, "Transcription not complete")

    stem = _safe_filename(Path(record["filename"]).stem) or "transcript"

    # --- PDF: Plus-only ---
    if fmt == "pdf":
        if (user.get("plan") or "free") != "plus":
            raise HTTPException(402, "PDF export is a Plus feature. Upgrade to unlock.")
        from weasyprint import HTML as _HTML, CSS as _CSS  # noqa: F401
        import json as _json
        from datetime import datetime as _dt

        # Build the segment rows with timestamps + speaker colors
        palette = ["#8b5cf6", "#6366f1", "#14b8a6", "#f59e0b", "#ef4444",
                   "#ec4899", "#10b981", "#f97316"]
        segments_raw = _json.loads(record.get("transcript_segments_json") or "[]")
        speaker_to_color: dict[str, str] = {}
        rows = []
        for seg in segments_raw:
            spk = seg.get("speaker") or ""
            if spk and spk not in speaker_to_color:
                speaker_to_color[spk] = palette[len(speaker_to_color) % len(palette)]
            total = int(seg.get("start") or 0)
            hh = total // 3600
            mm = (total % 3600) // 60
            ss = total % 60
            ts = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
            rows.append({
                "ts": ts,
                "speaker": spk,
                "speaker_color": speaker_to_color.get(spk),
                "text": (seg.get("text") or "").strip(),
            })

        speaker_chips = [{"name": name, "color": color}
                          for name, color in speaker_to_color.items()]

        duration_label = None
        if record.get("duration_seconds"):
            ds = int(record["duration_seconds"])
            if ds >= 3600:
                duration_label = f"{ds // 3600}h {(ds % 3600) // 60}m"
            else:
                duration_label = f"{ds // 60}m {ds % 60}s"

        created_label = ""
        try:
            dt = _dt.fromisoformat(record["created_at"].replace("Z", "+00:00"))
            created_label = dt.strftime("%B %-d, %Y")
        except Exception:
            created_label = (record.get("created_at") or "")[:10]

        html_body = templates.get_template("pdf/transcript.html").render({
            "filename": record.get("filename") or "Transcript",
            "created_label": created_label,
            "duration_label": duration_label,
            "speaker_count": len(speaker_to_color),
            "recap": (record.get("recap") or "").strip() or None,
            "speaker_chips": speaker_chips,
            "segments": rows,
        })
        pdf_bytes = _HTML(string=html_body, base_url=str(Path.cwd())).write_pdf()

        filename = f"{stem}.pdf"
        disposition = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
        return Response(
            content=pdf_bytes,
            headers={"Content-Disposition": disposition},
            media_type="application/pdf",
        )

    # --- Plain text formats ---
    field_map = {"txt": "transcript_text", "srt": "transcript_srt", "vtt": "transcript_vtt"}
    content = record[field_map[fmt]] or ""
    filename = f"{stem}.{fmt}"
    # RFC 5987 encoding handles non-ASCII and guards against header injection
    disposition = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"

    return PlainTextResponse(
        content,
        headers={"Content-Disposition": disposition},
        media_type="text/plain",
    )


@app.get("/api/transcriptions/{job_id}/vtt-inline")
async def vtt_inline(job_id: str, user: dict = Depends(auth.require_user)):
    record = _require_owner(await db.get_transcription(job_id), user)
    if record["status"] != "done":
        raise HTTPException(400, "Transcription not complete")

    # Regenerate from segments_json on each call so caption-chunking rules
    # (short cues, single-line display) apply even to pre-existing transcripts.
    import json as _json
    from transcriber import generate_vtt
    if record.get("transcript_segments_json"):
        try:
            segments = _json.loads(record["transcript_segments_json"]) or []
            vtt = generate_vtt(segments)
        except Exception:
            vtt = record.get("transcript_vtt") or ""
    else:
        vtt = record.get("transcript_vtt") or ""

    return Response(content=vtt, media_type="text/vtt")


@app.patch("/api/transcriptions/{job_id}/speakers")
async def rename_speakers(job_id: str, request: Request, user: dict = Depends(auth.require_user)):
    """Rename speaker labels across all segments. Body: {"mapping": {"Speaker A": "Pete", ...}}"""
    import json as _json
    from transcriber import generate_srt, generate_vtt

    record = _require_owner(await db.get_transcription(job_id), user)
    if not record.get("transcript_segments_json"):
        raise HTTPException(400, "Transcription has no segments to rename")

    body = await request.json()
    mapping = body.get("mapping") or {}
    if not isinstance(mapping, dict) or not mapping:
        raise HTTPException(400, "mapping must be a non-empty object")

    # Sanity: cap name length, reject CR/LF
    clean: dict[str, str] = {}
    for old, new in mapping.items():
        if not isinstance(old, str) or not isinstance(new, str):
            continue
        new = new.strip()
        if not new:
            continue
        if "\r" in new or "\n" in new or len(new) > 80:
            raise HTTPException(400, "Speaker names must be ≤80 chars and without line breaks")
        clean[old] = new
    if not clean:
        raise HTTPException(400, "No valid renames in mapping")

    try:
        segments = _json.loads(record["transcript_segments_json"]) or []
    except Exception:
        raise HTTPException(500, "Stored segments are corrupt")

    changed = 0
    for seg in segments:
        label = seg.get("speaker")
        if label and label in clean:
            seg["speaker"] = clean[label]
            changed += 1

    new_segments_json = _json.dumps(segments)
    new_srt = generate_srt(segments)
    new_vtt = generate_vtt(segments)

    await db.update_transcription(
        job_id,
        transcript_segments_json=new_segments_json,
        transcript_srt=new_srt,
        transcript_vtt=new_vtt,
    )
    return {"ok": True, "segments_updated": changed, "mapping": clean}


@app.patch("/api/transcriptions/{job_id}")
async def rename_transcription(job_id: str, filename: str = Form(...), user: dict = Depends(auth.require_user)):
    _require_owner(await db.get_transcription(job_id), user)
    clean = _safe_filename(filename)
    if not clean:
        raise HTTPException(400, "Filename cannot be empty")
    await db.update_transcription(job_id, filename=clean)
    return {"ok": True, "filename": clean}


@app.delete("/api/transcriptions/{job_id}")
async def delete_transcription(job_id: str, user: dict = Depends(auth.require_user)):
    record = _require_owner(await db.get_transcription(job_id), user)

    # Clean up video file if stored
    if record.get("video_path"):
        Path(record["video_path"]).unlink(missing_ok=True)

    await db.delete_transcription(job_id)
    return {"ok": True}


@app.post("/api/transcriptions/{job_id}/retry")
async def retry_transcription(job_id: str, user: dict = Depends(auth.require_user)):
    record = _require_owner(await db.get_transcription(job_id), user)
    if record["status"] != "error":
        raise HTTPException(400, "Only failed transcriptions can be retried")
    if not record.get("video_path") or not Path(record["video_path"]).exists():
        raise HTTPException(409, "Original video file no longer available. Please re-upload.")

    await db.update_transcription(
        job_id,
        status="pending",
        progress=0,
        error_message=None,
        retry_count=(record.get("retry_count") or 0) + 1,
    )

    video_path = Path(record["video_path"])
    _track_transcription(asyncio.create_task(_run_with_semaphore(job_id, video_path)))

    return {"id": job_id, "status": "pending"}


@app.get("/api/search")
async def search_transcriptions(q: str = "", user: dict = Depends(auth.require_user)):
    if not q or len(q) < 2:
        return []
    return await db.search_transcriptions(q, user_id=user["user_id"])


@app.post("/api/transcriptions/{job_id}/video")
async def upload_video_preview(job_id: str, file: UploadFile, user: dict = Depends(auth.require_user)):
    _require_owner(await db.get_transcription(job_id), user)

    ext = Path(file.filename).suffix.lower() if file.filename else ".mp4"
    if ext not in VIDEO_PREVIEW_EXTENSIONS:
        raise HTTPException(400, f"Unsupported preview format {ext}. Allowed: {', '.join(sorted(VIDEO_PREVIEW_EXTENSIONS))}")
    save_path = UPLOAD_DIR / f"{job_id}_preview{ext}"

    size = 0
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    with open(save_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                save_path.unlink(missing_ok=True)
                raise HTTPException(413, f"File too large. Maximum size is {MAX_UPLOAD_MB}MB")
            f.write(chunk)

    await db.update_transcription(job_id, video_path=str(save_path))
    return {"ok": True}


@app.get("/api/transcriptions/{job_id}/video")
async def get_video(job_id: str, user: dict = Depends(auth.require_user)):
    record = _require_owner(await db.get_transcription(job_id), user)
    if not record.get("video_path") or not Path(record["video_path"]).exists():
        raise HTTPException(404, "Video file not found")

    video_path = Path(record["video_path"])
    ext = video_path.suffix.lower()
    media_types = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
    }
    media_type = media_types.get(ext, "video/mp4")

    return FileResponse(video_path, media_type=media_type)


@app.delete("/api/transcriptions/{job_id}/video")
async def delete_video(job_id: str, user: dict = Depends(auth.require_user)):
    record = _require_owner(await db.get_transcription(job_id), user)
    if record.get("video_path"):
        Path(record["video_path"]).unlink(missing_ok=True)
        await db.update_transcription(job_id, video_path=None)
    return {"ok": True}


@app.post("/api/transcriptions/{job_id}/recap")
async def get_or_generate_recap(job_id: str, regenerate: bool = False, user: dict = Depends(auth.require_user)):
    record = _require_owner(await db.get_transcription(job_id), user)
    if record["status"] != "done":
        raise HTTPException(400, "Transcription not complete")

    if record.get("recap") and not regenerate:
        return {"recap": record["recap"]}

    from transcriber import generate_recap as gen_recap
    from openai import AsyncOpenAI
    client = AsyncOpenAI()

    transcript = record["transcript_text"] or ""
    try:
        recap = await gen_recap(transcript, client)
    except Exception as e:
        logger.warning("On-demand recap failed for %s: %s", job_id, e)
        await db.update_transcription(job_id, recap_status="failed")
        raise HTTPException(500, "Failed to generate recap. Check that your OpenAI key has access to chat models.")

    if not recap:
        await db.update_transcription(job_id, recap_status="failed")
        raise HTTPException(500, "Failed to generate recap. Check that your OpenAI key has access to chat models.")

    await db.update_transcription(job_id, recap=recap, recap_status="ok")
    return {"recap": recap}


@app.post("/api/send-email")
async def send_email(
    to: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    user: dict = Depends(auth.require_user),
):
    import html as _html
    from email_service import (
        FROM_ADDRESS,
        build_from_with_name,
        extract_from_email,
        send_recap_email,
    )

    to = _validate_email(to)
    subject = _reject_crlf(subject.strip(), "subject")
    if not subject:
        raise HTTPException(400, "Subject cannot be empty")
    if len(subject) > MAX_EMAIL_SUBJECT_LEN:
        raise HTTPException(400, f"Subject too long (max {MAX_EMAIL_SUBJECT_LEN} chars)")
    if len(body) > MAX_EMAIL_BODY_LEN:
        raise HTTPException(400, f"Body too long (max {MAX_EMAIL_BODY_LEN} chars)")

    plan = user.get("plan") or "free"
    hide_branding = bool(user.get("email_branding_hidden")) and plan == "plus"
    signature = (user.get("email_signature") or "").strip()
    full_name = (user.get("full_name") or "").strip()

    # Plain-text fallback — blank line between body, signature, and footer so
    # text-only clients still feel intentional.
    text_parts = [body.rstrip()]
    if signature:
        text_parts.append(signature)
    if not hide_branding:
        text_parts.append("\u2728 Sent with videoscriber.ai (https://videoscriber.ai)")
    body_text = "\n\n".join(text_parts)

    def _paragraphs(text: str) -> str:
        return "".join(
            f'<p style="margin:0 0 14px;">{_html.escape(line) or "&nbsp;"}</p>'
            for line in text.split("\n")
        )

    content = [f'<div>{_paragraphs(body.rstrip())}</div>']
    if signature:
        content.append(
            '<div style="margin-top:24px;padding-top:16px;'
            'border-top:1px solid #e5e7eb;color:#1f2330;">'
            f'{_paragraphs(signature)}</div>'
        )
    if not hide_branding:
        content.append(
            '<div style="margin-top:20px;padding-top:12px;'
            'border-top:1px solid #eef0f4;color:#94a3b8;font-size:12px;'
            'line-height:1.5;">'
            '\u2728 Sent with '
            '<a href="https://videoscriber.ai" '
            'style="color:#8B5CF6;text-decoration:none;font-weight:500;">'
            'videoscriber.ai</a>'
            '</div>'
        )
    body_html = (
        '<div style="'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
        'Roboto,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.6;color:#1f2330;'
        'max-width:640px;">'
        + "".join(content)
        + '</div>'
    )

    # From-header: user's name + verified sending address (custom domain when
    # available, otherwise the default Resend domain).
    base_from = _resolve_from_address(user) or FROM_ADDRESS
    from_override = build_from_with_name(full_name, extract_from_email(base_from))
    await send_recap_email(to, subject, body_text, body_html, from_override=from_override)
    return {"ok": True}


def _resolve_from_address(user: dict) -> str | None:
    """Return a Plus user's verified custom-domain from-address, else None.
    The send_email route decorates this with the user's name via
    email_service.build_from_with_name; we just pick the right sending host."""
    if (user.get("plan") or "free") != "plus":
        return None
    if user.get("custom_email_domain_status") != "verified":
        return None
    domain = (user.get("custom_email_domain") or "").strip()
    if not domain:
        return None
    return f"noreply@{domain}"


# ============================================================
# Cloud Sync API
# ============================================================

SYNC_KEY = os.getenv("SYNC_KEY", "")


def _check_sync_key(key: str):
    if not SYNC_KEY:
        raise HTTPException(503, "Sync not configured. Set SYNC_KEY in .env")
    if key != SYNC_KEY:
        raise HTTPException(401, "Invalid sync key")


@app.post("/api/sync/auth")
async def sync_auth(key: str = Form(...)):
    _check_sync_key(key)
    return {"ok": True}


@app.post("/api/sync/push")
async def sync_push(
    key: str = Form(...),
    id: str = Form(...),
    filename: str = Form(...),
    transcript_text: str = Form(default=""),
    transcript_srt: str = Form(default=""),
    transcript_vtt: str = Form(default=""),
    transcript_segments_json: str = Form(default=""),
    recap: str = Form(default=""),
    duration_seconds: float = Form(default=0),
    created_at: str = Form(default=""),
    completed_at: str = Form(default=""),
):
    _check_sync_key(key)

    existing = await db.get_transcription(id)
    if existing:
        await db.update_transcription(
            id,
            filename=filename,
            transcript_text=transcript_text or None,
            transcript_srt=transcript_srt or None,
            transcript_vtt=transcript_vtt or None,
            transcript_segments_json=transcript_segments_json or None,
            recap=recap or None,
        )
    else:
        await db.create_transcription(id, filename, 0)
        await db.update_transcription(
            id,
            status="done",
            progress=100,
            transcript_text=transcript_text or None,
            transcript_srt=transcript_srt or None,
            transcript_vtt=transcript_vtt or None,
            transcript_segments_json=transcript_segments_json or None,
            recap=recap or None,
            duration_seconds=duration_seconds,
            completed_at=completed_at or None,
        )

    return {"ok": True, "id": id}


@app.get("/api/sync/pull")
async def sync_pull(key: str, since: str = ""):
    _check_sync_key(key)
    all_records = await db.list_transcriptions()
    if since:
        all_records = [r for r in all_records if r.get("created_at", "") > since]
    # Strip video_path (local only) and return lightweight records
    for r in all_records:
        r.pop("video_path", None)
    return all_records


if __name__ == "__main__":
    # Reload mode is for local dev only — in the packaged desktop app the reloader
    # subprocess fights Electron's lifecycle management.
    uvicorn.run("app:app", host=HOST, port=PORT, reload=not DESKTOP_MODE)
