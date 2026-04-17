"""AI Assistant chat routes. Plus-plan only."""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncOpenAI

import auth
import database as db
import retrieval

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

CHAT_MODEL = "gpt-4o-mini"
MAX_HISTORY_MESSAGES = 20
TOP_K_CHUNKS = 8
SYSTEM_PROMPT = (
    "You are the Videoscriber AI Assistant. You help the user understand and extract "
    "insights from their meeting recordings and transcripts. Answer strictly based on "
    "the transcript excerpts provided in context. If context does not contain the answer, "
    "say so plainly. Cite recordings by filename when useful. Keep answers concise and "
    "actionable."
)


def _require_plus(user: dict) -> None:
    if (user.get("plan") or "free") != "plus":
        raise HTTPException(
            status_code=402,
            detail="The AI assistant is a Plus feature. Upgrade to chat with your library.",
        )


def _format_context(chunks: list[dict]) -> str:
    if not chunks:
        return "(No relevant excerpts were found in the user's library.)"
    lines = []
    for i, c in enumerate(chunks, start=1):
        ts = ""
        if c.get("start") is not None:
            mm = int((c["start"] or 0) // 60)
            ss = int((c["start"] or 0) % 60)
            ts = f" @ {mm:02d}:{ss:02d}"
        fn = c.get("filename") or "recording"
        lines.append(f"[{i}] {fn}{ts}\n{c['text']}")
    return "\n\n".join(lines)


@router.get("/conversations")
async def list_conversations(
    transcription_id: str | None = None,
    scope: str | None = None,
    user: dict = Depends(auth.require_user),
):
    _require_plus(user)
    return await db.list_chat_conversations(user["user_id"], scope=scope, transcription_id=transcription_id)


@router.post("/conversations")
async def create_conversation(
    scope: str = Form("library"),
    transcription_id: str = Form(default=""),
    title: str = Form(default=""),
    user: dict = Depends(auth.require_user),
):
    _require_plus(user)
    if scope not in ("library", "transcription"):
        raise HTTPException(400, "Invalid scope")
    if scope == "transcription":
        if not transcription_id:
            raise HTTPException(400, "transcription_id is required for transcription scope")
        # Verify ownership
        record = await db.get_transcription(transcription_id)
        if not record or record.get("user_id") != user["user_id"]:
            raise HTTPException(404, "Transcription not found")

    conv_id = str(uuid.uuid4())
    await db.create_chat_conversation(
        conv_id, user["user_id"], scope,
        transcription_id if scope == "transcription" else None,
        title.strip() or None,
    )
    return {"id": conv_id, "scope": scope, "transcription_id": transcription_id or None}


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str, user: dict = Depends(auth.require_user)):
    _require_plus(user)
    await db.delete_chat_conversation(conv_id, user["user_id"])
    return {"ok": True}


@router.get("/conversations/{conv_id}/messages")
async def get_messages(conv_id: str, user: dict = Depends(auth.require_user)):
    _require_plus(user)
    conv = await db.get_chat_conversation(conv_id, user["user_id"])
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return {
        "conversation": conv,
        "messages": await db.list_chat_messages(conv_id, limit=200),
    }


@router.post("/conversations/{conv_id}/messages")
async def send_message(
    conv_id: str,
    request: Request,
    message: str = Form(...),
    user: dict = Depends(auth.require_user),
):
    _require_plus(user)
    message = (message or "").strip()
    if not message:
        raise HTTPException(400, "Message cannot be empty")
    if len(message) > 8000:
        raise HTTPException(400, "Message too long (max 8000 characters)")

    conv = await db.get_chat_conversation(conv_id, user["user_id"])
    if not conv:
        raise HTTPException(404, "Conversation not found")

    # Save user message immediately
    await db.add_chat_message(str(uuid.uuid4()), conv_id, "user", message)

    # Retrieve context
    try:
        chunks = await retrieval.search_user_library(
            user["user_id"], message, top_k=TOP_K_CHUNKS,
            transcription_id=conv.get("transcription_id"),
        )
    except Exception as e:
        logger.warning("Retrieval failed for conv %s: %s", conv_id, e)
        chunks = []
    context = _format_context(chunks)

    # Load prior conversation history (bounded)
    prior = await db.list_chat_messages(conv_id, limit=MAX_HISTORY_MESSAGES)
    # Drop the message we just saved (it's the last one) — we'll add it explicitly below
    prior = [m for m in prior if m["content"] != message or m["role"] != "user"][:-0 or None]
    if prior and prior[-1]["role"] == "user" and prior[-1]["content"] == message:
        prior = prior[:-1]

    openai_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Relevant transcript excerpts:\n\n{context}"},
    ]
    for m in prior:
        if m["role"] in ("user", "assistant"):
            openai_messages.append({"role": m["role"], "content": m["content"]})
    openai_messages.append({"role": "user", "content": message})

    client = AsyncOpenAI()

    async def stream():
        full = []
        # First event: the sources we used (so the UI can show citations while text streams)
        srcs_payload = json.dumps({
            "sources": [
                {"transcription_id": c["transcription_id"], "filename": c["filename"],
                 "start": c.get("start"), "end": c.get("end")} for c in chunks
            ]
        })
        yield f"event: sources\ndata: {srcs_payload}\n\n"

        try:
            stream_resp = await client.chat.completions.create(
                model=CHAT_MODEL, messages=openai_messages, stream=True, temperature=0.3,
            )
            async for chunk in stream_resp:
                delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
                if delta:
                    full.append(delta)
                    payload = json.dumps({"delta": delta})
                    yield f"event: delta\ndata: {payload}\n\n"
            assistant_text = "".join(full).strip()
            if assistant_text:
                await db.add_chat_message(str(uuid.uuid4()), conv_id, "assistant", assistant_text)
            yield "event: done\ndata: {}\n\n"
        except Exception as e:
            logger.exception("Chat stream failed for conv %s", conv_id)
            err_payload = json.dumps({"error": "Assistant failed. Please try again."})
            yield f"event: error\ndata: {err_payload}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
