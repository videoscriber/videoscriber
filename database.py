import json
import re
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
    enhancement_error TEXT,
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
    stripe_subscription_id TEXT,
    plan_activated_at TEXT,
    custom_email_domain TEXT,
    custom_email_domain_id TEXT,
    custom_email_domain_status TEXT,
    email_signature TEXT,
    email_branding_hidden INTEGER DEFAULT 0,
    business TEXT,
    title TEXT
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

-- Integrations: per-user connections to Zoom, Google Meet, and local folders.
-- sync_mode is the user's toggle: 'off' (disabled), 'manual' (picker only),
-- or 'auto' (background sync, Plus-only). access/refresh tokens are stored
-- encrypted at rest using INTEGRATIONS_TOKEN_KEY in the env.
CREATE TABLE IF NOT EXISTS integrations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    account_label TEXT,
    access_token_encrypted BLOB,
    refresh_token_encrypted BLOB,
    token_expires_at TEXT,
    sync_mode TEXT NOT NULL DEFAULT 'manual',
    settings_json TEXT,
    last_sync_at TEXT,
    last_sync_status TEXT,
    last_sync_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_integrations_user_provider
    ON integrations(user_id, provider);

-- Imported-recording history. One row per external recording we've seen;
-- deduping happens via (integration_id, external_id). Status tracks the
-- pipeline from 'queued' through 'done' or 'error'.
CREATE TABLE IF NOT EXISTS integration_imports (
    id TEXT PRIMARY KEY,
    integration_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    external_title TEXT,
    transcription_id TEXT,
    status TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (integration_id) REFERENCES integrations(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_imports_integration_external
    ON integration_imports(integration_id, external_id);
CREATE INDEX IF NOT EXISTS idx_imports_user_recent
    ON integration_imports(user_id, created_at DESC);
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
    ("enhancement_error", "TEXT"),
    ("user_id", "TEXT"),
]

USER_MIGRATION_COLUMNS = [
    ("plan", "TEXT NOT NULL DEFAULT 'free'"),
    ("stripe_customer_id", "TEXT"),
    ("stripe_payment_method_id", "TEXT"),
    ("stripe_subscription_id", "TEXT"),
    ("plan_activated_at", "TEXT"),
    ("custom_email_domain", "TEXT"),
    ("custom_email_domain_id", "TEXT"),
    ("custom_email_domain_status", "TEXT"),
    ("email_signature", "TEXT"),
    ("email_branding_hidden", "INTEGER DEFAULT 0"),
    ("business", "TEXT"),
    ("title", "TEXT"),
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


_PARTIAL_UNIQUE_INDEX_RE = re.compile(
    r"CREATE\s+UNIQUE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"\S+\s+ON\s+(?P<table>\w+)\s*\(\s*(?P<column>\w+)\s*\)\s+"
    r"WHERE\s+\w+\s+IS\s+NOT\s+NULL",
    re.IGNORECASE,
)


def _partial_unique_indexes(schema_sql: str) -> list[tuple[str, str]]:
    """Return [(table, column), ...] for every partial-unique index in SCHEMA.
    Keeps `_migrate_enforce_unique_indexes` generic: add a UNIQUE INDEX and
    the next startup heals any pre-existing duplicates, no new code needed."""
    return [
        (m.group("table"), m.group("column"))
        for m in _PARTIAL_UNIQUE_INDEX_RE.finditer(schema_sql)
    ]


async def _migrate_enforce_unique_indexes(
    db: aiosqlite.Connection, schema_sql: str = SCHEMA
) -> None:
    """For every partial-unique index declared in SCHEMA, drop duplicate rows
    so the CREATE UNIQUE INDEX assertion in executescript() will succeed.
    Strategy: keep MIN(rowid) per non-null value (oldest row wins)."""
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cur:
        existing_tables = {row[0] async for row in cur}
    for table, column in _partial_unique_indexes(schema_sql):
        if table not in existing_tables:
            continue  # table will be fresh-created by SCHEMA; nothing to dedupe
        await db.execute(
            f"DELETE FROM {table} WHERE rowid NOT IN ("
            f"  SELECT MIN(rowid) FROM {table} "
            f"  WHERE {column} IS NOT NULL GROUP BY {column}"
            f") AND {column} IS NOT NULL"
        )
    await db.commit()


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await _migrate_users_phone_nullable(db)
        # Scans SCHEMA and auto-heals duplicates for every partial-unique
        # index, so new UNIQUE tightenings don't need a hand-written migrator.
        await _migrate_enforce_unique_indexes(db)
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
                        stripe_payment_method_id: str | None = None,
                        stripe_subscription_id: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        if plan == 'plus':
            await db.execute(
                "UPDATE users SET plan = ?, plan_activated_at = ?, "
                "stripe_customer_id = COALESCE(?, stripe_customer_id), "
                "stripe_payment_method_id = COALESCE(?, stripe_payment_method_id), "
                "stripe_subscription_id = COALESCE(?, stripe_subscription_id) "
                "WHERE id = ?",
                (plan, now, stripe_customer_id, stripe_payment_method_id,
                 stripe_subscription_id, user_id),
            )
        else:
            await db.execute(
                "UPDATE users SET plan = ?, plan_activated_at = NULL, "
                "stripe_subscription_id = NULL WHERE id = ?",
                (plan, user_id),
            )
        await db.commit()


async def set_user_stripe_customer(user_id: str, customer_id: str) -> None:
    """Link a Stripe Customer to a user (called before creating a Checkout Session)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
            (customer_id, user_id),
        )
        await db.commit()


async def get_user_by_stripe_customer(customer_id: str) -> dict | None:
    """Look up a user by their Stripe Customer ID — used by webhook handlers."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_transcription(id: str, **fields):
    if not fields:
        return
    # Whitelist allowed column names to prevent injection
    allowed = {
        "filename", "status", "progress", "error_message", "transcript_text", "transcript_srt",
        "transcript_vtt", "transcript_segments_json", "duration_seconds", "file_size",
        "total_chunks", "completed_chunks", "processing_started_at", "video_path",
        "retry_count", "recap", "recap_status", "speaker_id_status", "enhancement_status",
        "enhancement_error", "completed_at",
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


async def list_all_transcriptions_for_cleanup() -> list[dict]:
    """Cross-user SELECT used only by app._cleanup_orphans on startup. Returns
    the minimum shape the sweeper needs. Don't reach for this anywhere near a
    request handler — scoped reads must go through list_transcriptions."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, status, video_path FROM transcriptions"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


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


# ----------------------------------------------------------------------------
# Integrations (Zoom, Google Meet, local folder) — Phase 1 CRUD. OAuth flows
# + per-provider sync workers live alongside these helpers in later phases.
# ----------------------------------------------------------------------------

async def list_integrations(user_id: str) -> list[dict]:
    """All integrations for a user, most-recently-updated first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, user_id, provider, account_label, token_expires_at, "
            "sync_mode, settings_json, last_sync_at, last_sync_status, "
            "last_sync_error, created_at, updated_at "
            "FROM integrations WHERE user_id = ? "
            "ORDER BY updated_at DESC",
            (user_id,),
        ) as cursor:
            return [dict(row) async for row in cursor]


async def get_integration(integration_id: str, user_id: str) -> dict | None:
    """Single integration, only if it belongs to the caller."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM integrations WHERE id = ? AND user_id = ?",
            (integration_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_integration_by_provider(user_id: str, provider: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM integrations WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def upsert_integration(
    id: str,
    user_id: str,
    provider: str,
    *,
    account_label: str | None = None,
    access_token_encrypted: bytes | None = None,
    refresh_token_encrypted: bytes | None = None,
    token_expires_at: str | None = None,
    sync_mode: str = "manual",
    settings_json: str | None = None,
) -> None:
    """Insert-or-update by (user_id, provider). Preserves existing tokens when
    the caller omits them — useful when a user only toggles sync_mode."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        existing = None
        async with db.execute(
            "SELECT id, access_token_encrypted, refresh_token_encrypted, "
            "token_expires_at, settings_json "
            "FROM integrations WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        ) as cursor:
            existing = await cursor.fetchone()

        at_enc = access_token_encrypted if access_token_encrypted is not None else (
            existing[1] if existing else None
        )
        rt_enc = refresh_token_encrypted if refresh_token_encrypted is not None else (
            existing[2] if existing else None
        )
        exp = token_expires_at if token_expires_at is not None else (
            existing[3] if existing else None
        )
        settings = settings_json if settings_json is not None else (
            existing[4] if existing else None
        )

        if existing:
            await db.execute(
                "UPDATE integrations SET "
                "account_label = COALESCE(?, account_label), "
                "access_token_encrypted = ?, refresh_token_encrypted = ?, "
                "token_expires_at = ?, sync_mode = ?, settings_json = ?, "
                "updated_at = ? "
                "WHERE id = ?",
                (account_label, at_enc, rt_enc, exp, sync_mode, settings,
                 now, existing[0]),
            )
        else:
            await db.execute(
                "INSERT INTO integrations "
                "(id, user_id, provider, account_label, access_token_encrypted, "
                "refresh_token_encrypted, token_expires_at, sync_mode, "
                "settings_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (id, user_id, provider, account_label, at_enc, rt_enc, exp,
                 sync_mode, settings, now, now),
            )
        await db.commit()


async def update_integration_sync_state(
    integration_id: str,
    *,
    sync_mode: str | None = None,
    settings_json: str | None = None,
    last_sync_at: str | None = None,
    last_sync_status: str | None = None,
    last_sync_error: str | None = None,
) -> None:
    """Update only the sync-related columns. Any arg set to None is left alone."""
    fields = []
    params: list = []
    for col, val in (
        ("sync_mode", sync_mode),
        ("settings_json", settings_json),
        ("last_sync_at", last_sync_at),
        ("last_sync_status", last_sync_status),
        ("last_sync_error", last_sync_error),
    ):
        if val is not None:
            fields.append(f"{col} = ?")
            params.append(val)
    if not fields:
        return
    fields.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(integration_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE integrations SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        await db.commit()


async def delete_integration(integration_id: str, user_id: str) -> None:
    """Disconnect: removes the integration row; CASCADE removes its imports."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM integrations WHERE id = ? AND user_id = ?",
            (integration_id, user_id),
        )
        await db.commit()


async def list_integration_imports(integration_id: str, limit: int = 20) -> list[dict]:
    """Recent imports for a single integration, newest first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, external_id, external_title, transcription_id, status, "
            "error_message, created_at "
            "FROM integration_imports WHERE integration_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (integration_id, limit),
        ) as cursor:
            return [dict(row) async for row in cursor]


async def create_integration_import(
    id: str,
    integration_id: str,
    user_id: str,
    external_id: str,
    *,
    external_title: str | None = None,
    status: str = "queued",
) -> bool:
    """Insert an import row. Returns False if (integration_id, external_id) was
    already seen — callers treat that as "skip, dedupe hit"."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO integration_imports "
                "(id, integration_id, user_id, external_id, external_title, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (id, integration_id, user_id, external_id, external_title,
                 status, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def update_integration_import(
    import_id: str,
    *,
    status: str | None = None,
    transcription_id: str | None = None,
    error_message: str | None = None,
) -> None:
    fields = []
    params: list = []
    for col, val in (
        ("status", status),
        ("transcription_id", transcription_id),
        ("error_message", error_message),
    ):
        if val is not None:
            fields.append(f"{col} = ?")
            params.append(val)
    if not fields:
        return
    params.append(import_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE integration_imports SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        await db.commit()


async def queue_stats() -> dict:
    """Current queue state: how many jobs are queued and how many are actively running."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status, COUNT(*) FROM transcriptions "
            "WHERE status IN ('pending', 'extracting', 'transcribing') "
            "GROUP BY status"
        ) as cursor:
            counts = {row[0]: row[1] async for row in cursor}
    return {
        "pending": counts.get("pending", 0),
        "running": counts.get("extracting", 0) + counts.get("transcribing", 0),
    }
