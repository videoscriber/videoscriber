# Contributing

## Project Overview

Videoscriber — an AI-powered video transcription tool with a FastAPI backend and vanilla JS frontend. Runs as a web app (Docker) or Mac desktop app (Electron).

## Quick Commands

```bash
# Run locally
source .venv/bin/activate && python app.py

# Run with Docker
docker compose up --build

# Run Electron desktop app (dev)
cd desktop && npm start

# Syntax check
python -c "import ast; ast.parse(open('app.py').read()); ast.parse(open('transcriber.py').read()); ast.parse(open('database.py').read()); print('OK')"
```

## Architecture

3 Python files, no framework magic:
- `app.py` — FastAPI endpoints + config
- `transcriber.py` — ffmpeg + Whisper/AssemblyAI + GPT post-processing
- `database.py` — SQLite schema + async CRUD (aiosqlite)

Frontend is vanilla JS ES modules in `static/` — no build step.
Desktop wrapper is Electron in `desktop/` — spawns Python as subprocess.

## Key Patterns

- Transcription runs as `asyncio.create_task` with a `Semaphore` for concurrency
- Post-processing (speaker names, recap, video enhance) runs in parallel after job is marked "done"
- All post-processing is non-fatal — errors are logged but don't fail the job
- DB migrations auto-run in `init_db()` — add columns to `MIGRATION_COLUMNS` list
- `update_transcription()` has a field whitelist — add new columns there too
- Video enhancement uses Apple VideoToolbox (hardware) with libx264 fallback
- `[hidden]` CSS attribute is enforced with `!important` to prevent display override bugs

## Environment Variables

Required: `OPENAI_API_KEY`
Optional: `ASSEMBLYAI_API_KEY`, `SMTP_*`, `SYNC_KEY`, `RECAP_MODEL`
See `.env.example` for full list.

## Don't Forget

- When adding DB columns: update `SCHEMA`, `MIGRATION_COLUMNS`, `allowed` set in `update_transcription()`, and `list_transcriptions()` SELECT
- The frontend uses `[hidden]` attribute — always pair with the global `[hidden] { display: none !important }` rule
- `initUpload()` is async — it fetches `/api/config` on startup
- Recap is cached in DB `recap` column — clear it when changing the prompt
