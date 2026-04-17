import asyncio
import logging
import os
import re
import shutil
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

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

import auth
import auth_routes
import database as db
from transcriber import process_transcription

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1000"))
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

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
        raise RuntimeError("OPENAI_API_KEY environment variable is required")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required but not found on PATH")

    UPLOAD_DIR.mkdir(exist_ok=True)
    AUDIO_DIR.mkdir(exist_ok=True)

    await db.init_db()
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
        retention_task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
app.include_router(auth_routes.router)


@app.middleware("http")
async def gate_api_behind_session(request: Request, call_next):
    """Require a valid session for /api/* routes. /api/sync/* uses its own key auth."""
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/sync"):
        user = await auth.current_user(request)
        if not user:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
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
    return templates.TemplateResponse(request, "settings.html", {"user": user})


@app.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request):
    user = await auth.current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    plan = user.get("plan") or "free"
    limits = _plan_limits(plan)
    return templates.TemplateResponse(request, "upgrade.html", {
        "user": user,
        "plan": plan,
        "limits": limits,
        "free_max_file_mb": FREE_MAX_FILE_MB,
        "plus_max_file_mb": PLUS_MAX_FILE_MB,
        "free_monthly_limit": FREE_MONTHLY_LIMIT,
    })


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return templates.TemplateResponse(request, "legal/terms.html")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return templates.TemplateResponse(request, "legal/privacy.html")


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/config")
async def get_config(user: dict = Depends(auth.require_user)):
    plan = user.get("plan") or "free"
    limits = _plan_limits(plan)
    used = await _usage_count(user["user_id"])
    return {
        "diarization_available": bool(os.getenv("ASSEMBLYAI_API_KEY")),
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

    # Enforce the monthly limit BEFORE accepting bytes (applies to free tier only)
    if limits["monthly_limit"] is not None:
        used = await _usage_count(user["user_id"])
        remaining = limits["monthly_limit"] - used - len(files)
        if used >= limits["monthly_limit"] or remaining < 0:
            raise HTTPException(
                402,
                f"Free plan allows {limits['monthly_limit']} transcriptions per {USAGE_WINDOW_DAYS} days. "
                f"Upgrade to remove the limit.",
            )

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

        await db.create_transcription(job_id, file.filename, size, user_id=user["user_id"])
        asyncio.create_task(_run_with_semaphore(job_id, save_path, use_diarize))
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
    if fmt not in ("txt", "srt", "vtt"):
        raise HTTPException(400, "Format must be txt, srt, or vtt")

    record = _require_owner(await db.get_transcription(job_id), user)
    if record["status"] != "done":
        raise HTTPException(400, "Transcription not complete")

    field_map = {"txt": "transcript_text", "srt": "transcript_srt", "vtt": "transcript_vtt"}
    content = record[field_map[fmt]] or ""
    stem = _safe_filename(Path(record["filename"]).stem) or "transcript"
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

    return Response(
        content=record["transcript_vtt"] or "",
        media_type="text/vtt",
    )


@app.patch("/api/transcriptions/{job_id}")
async def rename_transcription(job_id: str, filename: str = Form(...), user: dict = Depends(auth.require_user)):
    record = _require_owner(await db.get_transcription(job_id), user)
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
    asyncio.create_task(_run_with_semaphore(job_id, video_path))

    return {"id": job_id, "status": "pending"}


@app.get("/api/search")
async def search_transcriptions(q: str = "", user: dict = Depends(auth.require_user)):
    if not q or len(q) < 2:
        return []
    return await db.search_transcriptions(q, user_id=user["user_id"])


@app.post("/api/transcriptions/{job_id}/video")
async def upload_video_preview(job_id: str, file: UploadFile, user: dict = Depends(auth.require_user)):
    record = _require_owner(await db.get_transcription(job_id), user)

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
    import smtplib
    from email.message import EmailMessage

    to = _validate_email(to)
    subject = _reject_crlf(subject.strip(), "subject")
    if not subject:
        raise HTTPException(400, "Subject cannot be empty")
    if len(subject) > MAX_EMAIL_SUBJECT_LEN:
        raise HTTPException(400, f"Subject too long (max {MAX_EMAIL_SUBJECT_LEN} chars)")
    if len(body) > MAX_EMAIL_BODY_LEN:
        raise HTTPException(400, f"Body too long (max {MAX_EMAIL_BODY_LEN} chars)")

    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    from_addr = os.getenv("SMTP_FROM", smtp_user)

    if not all([smtp_host, smtp_user, smtp_pass]):
        raise HTTPException(400, "Email not configured. Set SMTP_HOST, SMTP_USER, and SMTP_PASS in .env")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg.set_content(body)

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(502, "SMTP authentication failed. Check SMTP_USER and SMTP_PASS.")
    except smtplib.SMTPException as e:
        logger.warning("SMTP failure: %s", e)
        raise HTTPException(502, "Failed to send email via SMTP server.")
    except OSError as e:
        logger.warning("SMTP connection failure: %s", e)
        raise HTTPException(502, "Could not connect to SMTP server.")

    return {"ok": True}


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
    uvicorn.run("app:app", host=HOST, port=PORT, reload=True)
