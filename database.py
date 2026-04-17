import json
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("data/transcriptions.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcriptions (
    id TEXT PRIMARY KEY,
    user_id TEXT,
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
    last_login_at TEXT,
    plan TEXT NOT NULL DEFAULT 'free',
    stripe_customer_id TEXT,
    stripe_payment_method_id TEXT,
    plan_activated_at TEXT,
    custom_email_domain TEXT
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

-- AI Assistant: conversations (scope='library' for all-library chat, 'transcription' for per-video chat).
CREATE TABLE IF NOT EXISTS chat_conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'library',
    transcription_id TEXT,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_conv_user ON chat_conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,        -- 'user' | 'assistant'
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_msg_conv ON chat_messages(conversation_id, created_at);

-- Retrieval chunks: pre-computed embeddings for RAG lookup over the user's library.
CREATE TABLE IF NOT EXISTS transcript_chunks (
    id TEXT PRIMARY KEY,
    transcription_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    start_time REAL,
    end_time REAL,
    embedding BLOB NOT NULL,   -- float32 bytes
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_user ON transcript_chunks(user_id);
CREATE INDEX IF NOT EXISTS idx_chunks_transcription ON transcript_chunks(transcription_id);

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
    ("user_id", "TEXT"),
]

USER_MIGRATION_COLUMNS = [
    ("plan", "TEXT NOT NULL DEFAULT 'free'"),
    ("stripe_customer_id", "TEXT"),
    ("stripe_payment_method_id", "TEXT"),
    ("plan_activated_at", "TEXT"),
    ("custom_email_domain", "TEXT"),
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

        # Migrate transcriptions: add new columns if missing
        async with db.execute("PRAGMA table_info(transcriptions)") as cursor:
            existing = {row[1] async for row in cursor}
        for col_name, col_type in MIGRATION_COLUMNS:
            if col_name not in existing:
                await db.execute(
                    f"ALTER TABLE transcriptions ADD COLUMN {col_name} {col_type}"
                )

        # Migrate users: add new columns if missing (plan, stripe_*, etc.)
        async with db.execute("PRAGMA table_info(users)") as cursor:
            existing_user = {row[1] async for row in cursor}
        for col_name, col_type in USER_MIGRATION_COLUMNS:
            if col_name not in existing_user:
                await db.execute(
                    f"ALTER TABLE users ADD COLUMN {col_name} {col_type}"
                )

        # Create indexes that reference migrated columns AFTER migrations ran.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_transcriptions_user_created "
            "ON transcriptions(user_id, created_at DESC)"
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


async def create_transcription(id: str, filename: str, file_size: int, user_id: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO transcriptions (id, user_id, filename, file_size, status, progress, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', 0, ?)",
            (id, user_id, filename, file_size, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def try_create_transcription_atomic(
    id: str,
    user_id: str,
    filename: str,
    file_size: int,
    monthly_limit: int | None,
    window_since_iso: str | None,
) -> bool:
    """Atomically check usage and insert. Returns False if the user is at/above
    their monthly limit. All concurrent callers serialize at BEGIN IMMEDIATE,
    so races across parallel uploads cannot overshoot the limit."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if monthly_limit is not None and window_since_iso is not None:
                async with db.execute(
                    "SELECT COUNT(*) FROM transcriptions "
                    "WHERE user_id = ? AND created_at >= ?",
                    (user_id, window_since_iso),
                ) as cur:
                    (count,) = await cur.fetchone()
                if count >= monthly_limit:
                    await db.rollback()
                    return False
            await db.execute(
                "INSERT INTO transcriptions (id, user_id, filename, file_size, status, progress, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', 0, ?)",
                (id, user_id, filename, file_size, now),
            )
            await db.commit()
            return True
        except Exception:
            try:
                await db.rollback()
            except Exception:
                pass
            raise


async def recover_orphaned_video_paths(upload_dir: str) -> int:
    """Heal transcriptions that finished (status='done') but whose post-processing
    was interrupted: the enhanced video file sits on disk, but video_path in the
    DB is NULL. Returns how many rows were repaired."""
    import pathlib
    up = pathlib.Path(upload_dir)
    if not up.exists():
        return 0
    repaired = 0
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM transcriptions WHERE status = 'done' AND video_path IS NULL"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        for row in rows:
            jid = row["id"]
            enhanced = up / f"{jid}_enhanced.mp4"
            original = up / f"{jid}.mp4"
            if enhanced.exists():
                path = str(enhanced)
                status = "ok"
            elif original.exists():
                path = str(original)
                status = "skipped"
            else:
                continue
            await db.execute(
                "UPDATE transcriptions SET video_path = ?, enhancement_status = COALESCE(enhancement_status, ?) WHERE id = ?",
                (path, status, jid),
            )
            repaired += 1
        if repaired:
            await db.commit()
    return repaired


async def count_transcriptions_since(user_id: str, since_iso: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM transcriptions WHERE user_id = ? AND created_at >= ?",
            (user_id, since_iso),
        ) as cur:
            (count,) = await cur.fetchone()
            return count


async def find_free_transcriptions_older_than(cutoff_iso: str) -> list[dict]:
    """Return {id, video_path} for transcriptions owned by free-plan users older than cutoff."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT t.id, t.video_path FROM transcriptions t "
            "JOIN users u ON u.id = t.user_id "
            "WHERE u.plan = 'free' AND t.created_at < ?",
            (cutoff_iso,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def insert_chunks(chunks: list[tuple]) -> None:
    """Each row: (id, transcription_id, user_id, chunk_index, text, start_time, end_time, embedding_bytes, created_at)"""
    if not chunks:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT INTO transcript_chunks "
            "(id, transcription_id, user_id, chunk_index, text, start_time, end_time, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            chunks,
        )
        await db.commit()


async def delete_chunks_for_transcription(transcription_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM transcript_chunks WHERE transcription_id = ?",
            (transcription_id,),
        )
        await db.commit()


async def load_user_chunks(user_id: str, transcription_id: str | None = None) -> list[dict]:
    """Load all chunks for a user (or a single transcription). Used by the in-memory vector search."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if transcription_id:
            query = (
                "SELECT c.*, t.filename FROM transcript_chunks c "
                "JOIN transcriptions t ON t.id = c.transcription_id "
                "WHERE c.user_id = ? AND c.transcription_id = ?"
            )
            params = (user_id, transcription_id)
        else:
            query = (
                "SELECT c.*, t.filename FROM transcript_chunks c "
                "JOIN transcriptions t ON t.id = c.transcription_id "
                "WHERE c.user_id = ?"
            )
            params = (user_id,)
        async with db.execute(query, params) as cur:
            return [dict(row) for row in await cur.fetchall()]


# ----- Chat conversations and messages ---------------------------------------

async def create_chat_conversation(conv_id: str, user_id: str, scope: str,
                                    transcription_id: str | None, title: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_conversations (id, user_id, scope, transcription_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conv_id, user_id, scope, transcription_id, title, now, now),
        )
        await db.commit()


async def get_chat_conversation(conv_id: str, user_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM chat_conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_chat_conversations(user_id: str, scope: str | None = None,
                                    transcription_id: str | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        clauses = ["user_id = ?"]
        params: list = [user_id]
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if transcription_id:
            clauses.append("transcription_id = ?")
            params.append(transcription_id)
        q = (
            "SELECT id, scope, transcription_id, title, created_at, updated_at "
            "FROM chat_conversations WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC"
        )
        async with db.execute(q, tuple(params)) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def delete_chat_conversation(conv_id: str, user_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM chat_messages WHERE conversation_id = ? AND conversation_id IN "
            "(SELECT id FROM chat_conversations WHERE user_id = ?)",
            (conv_id, user_id),
        )
        await db.execute(
            "DELETE FROM chat_conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        )
        await db.commit()


async def add_chat_message(message_id: str, conv_id: str, role: str, content: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_messages (id, conversation_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, conv_id, role, content, now),
        )
        await db.execute(
            "UPDATE chat_conversations SET updated_at = ? WHERE id = ?",
            (now, conv_id),
        )
        await db.commit()


async def list_chat_messages(conv_id: str, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content, created_at FROM chat_messages "
            "WHERE conversation_id = ? ORDER BY created_at ASC LIMIT ?",
            (conv_id, limit),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def delete_transcriptions_bulk(ids: list[str]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"DELETE FROM transcriptions WHERE id IN ({placeholders})", ids)
        await db.commit()


async def set_user_plan(user_id: str, plan: str,
                        stripe_customer_id: str | None = None,
                        stripe_payment_method_id: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        if plan == 'plus':
            await db.execute(
                "UPDATE users SET plan = ?, plan_activated_at = ?, "
                "stripe_customer_id = COALESCE(?, stripe_customer_id), "
                "stripe_payment_method_id = COALESCE(?, stripe_payment_method_id) "
                "WHERE id = ?",
                (plan, now, stripe_customer_id, stripe_payment_method_id, user_id),
            )
        else:
            await db.execute(
                "UPDATE users SET plan = ?, plan_activated_at = NULL WHERE id = ?",
                (plan, user_id),
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


async def list_transcriptions(user_id: str | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if user_id is None:
            return []
        async with db.execute(
            "SELECT id, filename, status, progress, error_message, duration_seconds, "
            "file_size, total_chunks, completed_chunks, processing_started_at, "
            "video_path, retry_count, recap, recap_status, speaker_id_status, "
            "enhancement_status, created_at, completed_at "
            "FROM transcriptions WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def search_transcriptions(query: str, user_id: str | None = None) -> list[dict]:
    results = []
    if user_id is None:
        return results
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, filename, transcript_segments_json FROM transcriptions "
            "WHERE user_id = ? AND status = 'done' AND transcript_segments_json IS NOT NULL",
            (user_id,),
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
