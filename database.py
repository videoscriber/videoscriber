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


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(SCHEMA)

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
