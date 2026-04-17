# Videoscriber

AI-powered video transcription with speaker identification, synced subtitles, and automatic recap emails.

## Features

- **Upload & transcribe** videos/audio (MP4, MOV, AVI, MKV, WebM, MP3, WAV, FLAC)
- **Speaker identification** via AssemblyAI with AI-powered name detection
- **Video preview** with synced subtitles and hardware-accelerated enhancement
- **Recap emails** auto-generated with GPT — witty, professional, actionable
- **Search** across all transcriptions (Cmd+K)
- **Export** as Text, SRT, or VTT subtitles
- **Rename** transcriptions inline
- **Keyboard shortcuts** for power users
- **Dark/light mode** with premium UI
- **Cloud sync** between desktop and web instances
- **Desktop app** via Electron (Mac)

## Quick Start

### Prerequisites

- Python 3.10+
- ffmpeg (`brew install ffmpeg` on Mac)
- An OpenAI API key

### Setup

```bash
git clone <repo-url> && cd video-transcriber

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY

# Run
python app.py
```

Open http://127.0.0.1:8000

### Docker

```bash
cp .env.example .env
# Edit .env with your keys

docker compose up --build
```

Open http://localhost:8000

## Architecture

```
video-transcriber/
├── app.py              # FastAPI application — all API endpoints
├── transcriber.py      # Transcription pipeline — ffmpeg, Whisper, AssemblyAI
├── database.py         # SQLite schema + async CRUD
├── templates/
│   └── index.html      # Single-page UI
├── static/
│   ├── style.css       # Design system + layout
│   ├── components.css  # Buttons, modals, toasts
│   ├── transcript.css  # Transcript viewer, video player
│   ├── app.js          # Main app logic
│   ├── upload.js       # File upload + drag-and-drop
│   ├── transcript.js   # Segment rendering + video sync
│   ├── toast.js        # Notifications
│   └── theme.js        # Dark/light mode
├── desktop/            # Electron Mac app
├── Dockerfile
├── docker-compose.yml
└── .github/workflows/  # CI/CD
```

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python, FastAPI, Uvicorn |
| Database | SQLite (async via aiosqlite) |
| Transcription | OpenAI Whisper API |
| Diarization | AssemblyAI (optional) |
| AI Features | GPT-4o-mini (speaker names, recaps) |
| Audio/Video | ffmpeg (extraction, chunking, enhancement) |
| Frontend | Vanilla JS, CSS (no framework) |
| Desktop | Electron |

### Processing Pipeline

```
Upload → Extract Audio (ffmpeg) → Chunk if >25MB → Transcribe (Whisper/AssemblyAI)
    → Generate Text/SRT/VTT → [Post-process in parallel:]
        → Identify speaker names (GPT)
        → Generate recap email (GPT)
        → Enhance video quality (ffmpeg + VideoToolbox)
```

## API Reference

### Core Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serve the web UI |
| `GET` | `/api/config` | Feature flags (`{diarization_available}`) |
| `POST` | `/api/upload` | Upload files. Form fields: `files` (multipart), `diarize` (optional "true") |
| `GET` | `/api/transcriptions` | List all transcriptions |
| `GET` | `/api/transcriptions/{id}` | Get single transcription with full text |
| `PATCH` | `/api/transcriptions/{id}` | Rename. Form field: `filename` |
| `DELETE` | `/api/transcriptions/{id}` | Delete transcription and files |
| `POST` | `/api/transcriptions/{id}/retry` | Retry a failed transcription |
| `GET` | `/api/transcriptions/{id}/download/{fmt}` | Download as `txt`, `srt`, or `vtt` |
| `GET` | `/api/transcriptions/{id}/vtt-inline` | VTT for `<track>` element (`text/vtt`) |

### Video Preview

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/transcriptions/{id}/video` | Upload video for preview. Form field: `file` |
| `GET` | `/api/transcriptions/{id}/video` | Stream video (supports Range headers) |
| `DELETE` | `/api/transcriptions/{id}/video` | Remove stored video |

### AI Features

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/transcriptions/{id}/recap` | Generate or retrieve cached recap. Query param: `regenerate=true` |
| `GET` | `/api/search?q=term` | Search across all transcripts (min 2 chars) |
| `POST` | `/api/send-email` | Send recap via Resend. Form fields: `to`, `subject`, `body` |

### Cloud Sync

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/sync/auth` | Validate sync key. Form field: `key` |
| `POST` | `/api/sync/push` | Push transcription to cloud. Form fields: `key`, `id`, `filename`, `transcript_text`, etc. |
| `GET` | `/api/sync/pull?key=...&since=...` | Pull transcriptions from cloud |

## Configuration

All configuration is via environment variables (`.env` file):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENAI_API_KEY` | Yes | — | OpenAI API key (Whisper + GPT) |
| `ASSEMBLYAI_API_KEY` | No | — | Enables speaker diarization |
| `RESEND_API_KEY` | No | — | Resend key; powers OTP + recap email + custom-domain sending |
| `RESEND_FROM` | No | `VideoScriber <auth@videoscriber.ai>` | Default From address for outbound mail |
| `SYNC_KEY` | No | — | Shared secret for cloud sync |
| `HOST` | No | `127.0.0.1` | Bind address |
| `PORT` | No | `8000` | Bind port |
| `MAX_UPLOAD_MB` | No | `1000` | Max upload size in MB |
| `MAX_CONCURRENT_JOBS` | No | `2` | Parallel transcription limit |
| `RECAP_MODEL` | No | `gpt-4o-mini` | GPT model for recaps |
| `KEEP_VIDEO_FOR_PREVIEW` | No | `true` | Keep & enhance video after transcription |

## Desktop App (Mac)

### Development

```bash
# From project root — ensure Python deps are installed
source .venv/bin/activate
pip install -r requirements.txt

# Install Electron deps
cd desktop
npm install

# Run in dev mode (uses system Python + ffmpeg)
npm start
```

### Build .dmg

```bash
# Bundle Python environment
bash desktop/scripts/bundle-python.sh

# Build the app
cd desktop
npm run make
```

The `.dmg` will be in `desktop/out/make/`.

### How It Works

The Electron app spawns the Python backend as a child process on a random port, then loads the web UI in a native Mac window. Data is stored in `~/Library/Application Support/Videoscriber/`.

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Cmd+K` | Open global search |
| `D` | Toggle dark/light mode |
| `J` / `K` | Navigate transcription list |
| `Enter` | Open selected transcription |
| `C` | Copy transcript |
| `1` / `2` / `3` / `4` | Switch format (Segments/Text/SRT/VTT) |
| `Esc` | Close modal / deselect |
| `?` | Show shortcut reference |

## Contributing

### Project Setup

```bash
git clone <repo-url> && cd video-transcriber
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Add your OPENAI_API_KEY to .env
python app.py
```

### Code Structure

- **Backend** is 3 Python files — `app.py` (endpoints), `transcriber.py` (processing), `database.py` (storage)
- **Frontend** is vanilla JS with ES modules — no build step, no npm for the web app
- **CSS** uses custom properties for theming — all colors defined in `:root` in `style.css`
- **Database** migrations run automatically in `init_db()` — add new columns to `MIGRATION_COLUMNS`

### Adding a New Endpoint

1. Add the route in `app.py`
2. Add any new DB fields to `SCHEMA`, `MIGRATION_COLUMNS`, and the `allowed` set in `update_transcription()`
3. Update the frontend JS to call it

### Adding a New Feature to the UI

1. Add HTML in `templates/index.html`
2. Add styles in the appropriate CSS file (`style.css` for layout, `components.css` for widgets, `transcript.css` for transcript-related)
3. Wire it up in the appropriate JS module
