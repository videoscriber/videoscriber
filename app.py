import asyncio
import logging
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

import auth
import auth_routes
import database as db
from transcriber import process_transcription

load_dotenv()

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
    logger.info("Videoscriber ready at http://%s:%s", HOST, PORT)
    yield


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
    return templates.TemplateResponse(request, "index.html", {"user": user})


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
    return {
        "diarization_available": bool(os.getenv("ASSEMBLYAI_API_KEY")),
        "user": {
            "id": user["user_id"],
            "full_name": user.get("full_name"),
            "email": user.get("email"),
            "phone": user["phone"],
        },
    }


@app.post("/api/upload")
async def upload_video(files: list[UploadFile], diarize: str = Form(default="false")):
    use_diarize = diarize.lower() == "true"
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
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    save_path.unlink(missing_ok=True)
                    raise HTTPException(413, f"File too large. Maximum size is {MAX_UPLOAD_MB}MB")
                f.write(chunk)

        await db.create_transcription(job_id, file.filename, size)
        asyncio.create_task(_run_with_semaphore(job_id, save_path, use_diarize))
        results.append({"id": job_id, "status": "pending"})

    return results


async def _run_with_semaphore(job_id: str, video_path: Path, diarize: bool = False):
    async with semaphore:
        await process_transcription(job_id, video_path, AUDIO_DIR, diarize=diarize)


@app.get("/api/transcriptions")
async def list_transcriptions():
    return await db.list_transcriptions()


@app.get("/api/transcriptions/{job_id}")
async def get_transcription(job_id: str):
    record = await db.get_transcription(job_id)
    if not record:
        raise HTTPException(404, "Transcription not found")
    return record


@app.get("/api/transcriptions/{job_id}/download/{fmt}")
async def download_transcription(job_id: str, fmt: str):
    if fmt not in ("txt", "srt", "vtt"):
        raise HTTPException(400, "Format must be txt, srt, or vtt")

    record = await db.get_transcription(job_id)
    if not record:
        raise HTTPException(404, "Transcription not found")
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
async def vtt_inline(job_id: str):
    record = await db.get_transcription(job_id)
    if not record:
        raise HTTPException(404, "Transcription not found")
    if record["status"] != "done":
        raise HTTPException(400, "Transcription not complete")

    return Response(
        content=record["transcript_vtt"] or "",
        media_type="text/vtt",
    )


@app.patch("/api/transcriptions/{job_id}")
async def rename_transcription(job_id: str, filename: str = Form(...)):
    record = await db.get_transcription(job_id)
    if not record:
        raise HTTPException(404, "Transcription not found")
    clean = _safe_filename(filename)
    if not clean:
        raise HTTPException(400, "Filename cannot be empty")
    await db.update_transcription(job_id, filename=clean)
    return {"ok": True, "filename": clean}


@app.delete("/api/transcriptions/{job_id}")
async def delete_transcription(job_id: str):
    record = await db.get_transcription(job_id)
    if not record:
        raise HTTPException(404, "Transcription not found")

    # Clean up video file if stored
    if record.get("video_path"):
        Path(record["video_path"]).unlink(missing_ok=True)

    await db.delete_transcription(job_id)
    return {"ok": True}


@app.post("/api/transcriptions/{job_id}/retry")
async def retry_transcription(job_id: str):
    record = await db.get_transcription(job_id)
    if not record:
        raise HTTPException(404, "Transcription not found")
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
async def search_transcriptions(q: str = ""):
    if not q or len(q) < 2:
        return []
    return await db.search_transcriptions(q)


@app.post("/api/transcriptions/{job_id}/video")
async def upload_video_preview(job_id: str, file: UploadFile):
    record = await db.get_transcription(job_id)
    if not record:
        raise HTTPException(404, "Transcription not found")

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
async def get_video(job_id: str):
    record = await db.get_transcription(job_id)
    if not record:
        raise HTTPException(404, "Transcription not found")
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
async def delete_video(job_id: str):
    record = await db.get_transcription(job_id)
    if not record:
        raise HTTPException(404, "Transcription not found")
    if record.get("video_path"):
        Path(record["video_path"]).unlink(missing_ok=True)
        await db.update_transcription(job_id, video_path=None)
    return {"ok": True}


@app.post("/api/transcriptions/{job_id}/recap")
async def get_or_generate_recap(job_id: str, regenerate: bool = False):
    record = await db.get_transcription(job_id)
    if not record:
        raise HTTPException(404, "Transcription not found")
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
