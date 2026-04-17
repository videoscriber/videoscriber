import json
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("data/transcriptions.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcriptions (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    progress INTEGER DEFAULT 0,
    error_message TEXT,
    transcript_text TEXT,
    transcript_srt TEXT,
    transcript_vtt TEXT,
    transcript_segments_json TEXT,
    duration_seconds REAL,
    file_size INTEGER,
    total_chunks INTEGER,
    completed_chunks INTEGER DEFAULT 0,
    processing_started_at TEXT,
    video_path TEXT,
    retry_count INTEGER DEFAULT 0,
    recap TEXT,
    recap_status TEXT,
    speaker_id_status TEXT,
    enhancement_status TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    phone TEXT,
    full_name TEXT,
    email TEXT,
    profile_completed_at TEXT,
    consented_tos_at TEXT,
    consented_tos_version TEXT,
    consented_privacy_at TEXT,
    consented_privacy_version TEXT,
    disabled_at TEXT,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
-- Partial unique indexes: phone or email can be null, but must be unique if present.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON users(phone) WHERE phone IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email) WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS email_otp_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    attempts INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_otp_phone ON email_otp_codes(phone, used_at);

-- Email-primary signin (no phone required). Used during beta / when AUTH_MODE=email.
CREATE TABLE IF NOT EXISTS email_signin_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    attempts INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_signin_email ON email_signin_codes(email, used_at);

CREATE TABLE IF NOT EXISTS otp_rate_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_otp_rate_key ON otp_rate_limits(key, action, created_at);
"""

MIGRATION_COLUMNS = [
    ("transcript_segments_json", "TEXT"),
    ("total_chunks", "INTEGER"),
    ("completed_chunks", "INTEGER DEFAULT 0"),
    ("processing_started_at", "TEXT"),
    ("video_path", "TEXT"),
    ("retry_count", "INTEGER DEFAULT 0"),
    ("recap", "TEXT"),
    ("recap_status", "TEXT"),
    ("speaker_id_status", "TEXT"),
    ("enhancement_status", "TEXT"),
]


async def _migrate_users_phone_nullable(db: aiosqlite.Connection) -> None:
    """Older DBs have users.phone NOT NULL. Rebuild the table without that constraint."""
    async with db.execute("PRAGMA table_info(users)") as cur:
        cols = [row async for row in cur]
    if not cols:
        return  # table doesn't exist — CREATE TABLE in SCHEMA will make it correctly
    phone_col = next((c for c in cols if c[1] == "phone"), None)
    if not phone_col or phone_col[3] == 0:
        return  # already nullable
    await db.executescript(
        "CREATE TABLE users_new ("
        "  id TEXT PRIMARY KEY,"
        "  phone TEXT,"
        "  full_name TEXT,"
        "  email TEXT,"
        "  profile_completed_at TEXT,"
        "  consented_tos_at TEXT,"
        "  consented_tos_version TEXT,"
        "  consented_privacy_at TEXT,"
        "  consented_privacy_version TEXT,"
        "  disabled_at TEXT,"
        "  created_at TEXT NOT NULL,"
        "  last_login_at TEXT"
        ");"
        "INSERT INTO users_new SELECT id, phone, full_name, email, profile_completed_at,"
        "  consented_tos_at, consented_tos_version, consented_privacy_at, consented_privacy_version,"
        "  disabled_at, created_at, last_login_at FROM users;"
        "DROP TABLE users;"
        "ALTER TABLE users_new RENAME TO users;"
        "CREATE UNIQUE INDEX idx_users_phone_unique ON users(phone) WHERE phone IS NOT NULL;"
        "CREATE UNIQUE INDEX idx_users_email_unique ON users(email) WHERE email IS NOT NULL;"
    )


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await _migrate_users_phone_nullable(db)
        await db.executescript(SCHEMA)

        # Migrate existing databases: add new columns if missing
        async with db.execute("PRAGMA table_info(transcriptions)") as cursor:
            existing = {row[1] async for row in cursor}

        for col_name, col_type in MIGRATION_COLUMNS:
            if col_name not in existing:
                await db.execute(
                    f"ALTER TABLE transcriptions ADD COLUMN {col_name} {col_type}"
                )

        # Mark any interrupted jobs as error on startup
        await db.execute(
            "UPDATE transcriptions SET status = 'error', error_message = 'Interrupted by server restart' "
            "WHERE status IN ('pending', 'extracting', 'transcribing')"
        )

        # Drop expired sessions on startup
        await db.execute(
            "DELETE FROM sessions WHERE expires_at < ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await db.commit()


async def create_transcription(id: str, filename: str, file_size: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO transcriptions (id, filename, file_size, status, progress, created_at) "
            "VALUES (?, ?, ?, 'pending', 0, ?)",
            (id, filename, file_size, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def update_transcription(id: str, **fields):
    if not fields:
        return
    # Whitelist allowed column names to prevent injection
    allowed = {
        "filename", "status", "progress", "error_message", "transcript_text", "transcript_srt",
        "transcript_vtt", "transcript_segments_json", "duration_seconds", "file_size",
        "total_chunks", "completed_chunks", "processing_started_at", "video_path",
        "retry_count", "recap", "recap_status", "speaker_id_status", "enhancement_status",
        "completed_at",
    }
    for k in fields:
        if k not in allowed:
            raise ValueError(f"Invalid field: {k}")

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE transcriptions SET {set_clause} WHERE id = ?", values
        )
        await db.commit()


async def get_transcription(id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM transcriptions WHERE id = ?", (id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_transcriptions() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, filename, status, progress, error_message, duration_seconds, "
            "file_size, total_chunks, completed_chunks, processing_started_at, "
            "video_path, retry_count, recap, recap_status, speaker_id_status, "
            "enhancement_status, created_at, completed_at "
            "FROM transcriptions ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def search_transcriptions(query: str) -> list[dict]:
    results = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, filename, transcript_segments_json FROM transcriptions "
            "WHERE status = 'done' AND transcript_segments_json IS NOT NULL"
        ) as cursor:
            async for row in cursor:
                row = dict(row)
                segments = json.loads(row["transcript_segments_json"])
                matches = []
                query_lower = query.lower()
                for i, seg in enumerate(segments):
                    if query_lower in seg["text"].lower():
                        matches.append({
                            "segment_index": i,
                            "text": seg["text"],
                            "start": seg["start"],
                            "end": seg["end"],
                            "speaker": seg.get("speaker"),
                        })
                if matches:
                    results.append({
                        "id": row["id"],
                        "filename": row["filename"],
                        "matches": matches[:10],  # Limit matches per transcription
                    })
    return results


async def delete_transcription(id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM transcriptions WHERE id = ?", (id,))
        await db.commit()
