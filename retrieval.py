"""Chunk transcripts, embed them, and do in-memory cosine-similarity search.

We keep embeddings inside SQLite (as float32 blobs) and do the nearest-neighbour
lookup in Python / numpy. This is fine for our scale (a typical user has
thousands of chunks, well under the memory budget). Swap in sqlite-vec or a
vector DB later if we ever hit the ceiling.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import numpy as np
from openai import AsyncOpenAI

import database as db

logger = logging.getLogger(__name__)

import os
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = 1536
# Target chunk size (characters) — roughly 250-400 tokens for English transcripts.
CHUNK_TARGET_CHARS = 1400
CHUNK_OVERLAP_CHARS = 200


def _pack(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def _unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def chunk_segments(segments: list[dict]) -> list[dict]:
    """Group transcript segments into ~CHUNK_TARGET_CHARS-sized chunks with timestamps.

    segments is the list from transcript_segments_json; each element has
    keys like {text, start, end, speaker}.
    """
    chunks: list[dict] = []
    buf: list[dict] = []
    buf_len = 0

    def flush():
        if not buf:
            return
        text = " ".join(s["text"].strip() for s in buf if s.get("text")).strip()
        if not text:
            buf.clear()
            return
        chunks.append({
            "text": text,
            "start": buf[0].get("start"),
            "end": buf[-1].get("end"),
        })
        buf.clear()

    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        seg_len = len(text) + 1
        if buf_len + seg_len > CHUNK_TARGET_CHARS and buf_len > 0:
            flush()
            buf_len = 0
        buf.append(seg)
        buf_len += seg_len

    flush()
    # If chunks are very long (no segment breaks), split them hard.
    out: list[dict] = []
    for c in chunks:
        t = c["text"]
        if len(t) <= CHUNK_TARGET_CHARS + 400:
            out.append(c)
            continue
        step = CHUNK_TARGET_CHARS - CHUNK_OVERLAP_CHARS
        for i in range(0, len(t), step):
            out.append({"text": t[i:i + CHUNK_TARGET_CHARS], "start": c["start"], "end": c["end"]})
    return out


async def _embed_batch(client: AsyncOpenAI, texts: list[str]) -> list[list[float]]:
    # OpenAI accepts up to 2048 inputs per call for text-embedding-3-small.
    resp = await client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [item.embedding for item in resp.data]


async def embed_and_store_transcription(transcription_id: str, user_id: str,
                                         segments_json: str | None,
                                         transcript_text: str | None) -> int:
    """Compute and store chunk embeddings for a completed transcription. Returns chunk count."""
    segments: list[dict] = []
    if segments_json:
        try:
            segments = json.loads(segments_json) or []
        except Exception:
            segments = []
    if not segments and transcript_text:
        segments = [{"text": transcript_text}]
    if not segments:
        return 0

    chunks = chunk_segments(segments)
    if not chunks:
        return 0

    client = AsyncOpenAI()
    texts = [c["text"] for c in chunks]
    # Batch in groups of 96 to stay well under API limits.
    embeddings: list[list[float]] = []
    for i in range(0, len(texts), 96):
        batch = texts[i:i + 96]
        embeddings.extend(await _embed_batch(client, batch))

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        vec = np.asarray(emb, dtype=np.float32)
        rows.append((
            str(uuid.uuid4()),
            transcription_id,
            user_id,
            i,
            chunk["text"],
            chunk.get("start"),
            chunk.get("end"),
            _pack(vec),
            now,
        ))
    # Replace any existing chunks for this transcription (re-embed on retry etc.)
    await db.delete_chunks_for_transcription(transcription_id)
    await db.insert_chunks(rows)
    logger.info("Embedded %d chunks for transcription %s", len(rows), transcription_id)
    return len(rows)


async def search_user_library(user_id: str, query: str, top_k: int = 8,
                               transcription_id: str | None = None) -> list[dict]:
    """Return top_k most relevant chunks for a query, across the user's library."""
    chunks = await db.load_user_chunks(user_id, transcription_id=transcription_id)
    if not chunks:
        return []
    client = AsyncOpenAI()
    q_vec = np.asarray((await _embed_batch(client, [query]))[0], dtype=np.float32)
    q_vec /= max(np.linalg.norm(q_vec), 1e-9)

    mat = np.stack([_unpack(c["embedding"]) for c in chunks], axis=0)
    mat_n = _normalize(mat)
    scores = mat_n @ q_vec  # cosine similarity
    order = np.argsort(-scores)[:top_k]
    results = []
    for idx in order:
        c = chunks[int(idx)]
        results.append({
            "transcription_id": c["transcription_id"],
            "filename": c.get("filename"),
            "text": c["text"],
            "start": c.get("start_time"),
            "end": c.get("end_time"),
            "score": float(scores[idx]),
        })
    return results
