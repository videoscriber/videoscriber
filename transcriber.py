"""Transcription pipeline.

File lifecycle (see app._cleanup_orphans for the startup sweep):

  uploads/{job_id}{ext}            original upload — kept until the job is
                                    deleted via the API, or replaced by an
                                    enhanced copy on successful post-process
  uploads/{job_id}_preview{ext}    preview-video uploaded separately after
                                    transcription completes
  uploads/{job_id}_enhanced.mp4    produced by enhance_video(); becomes the
                                    new video_path and the original is unlinked
  audio/{job_id}.mp3               transient — deleted in the finally block
  audio/{job_id}_chunk_NNN.mp3     transient — deleted in the finally block

On error the original video is kept and video_path is re-set so /retry works.
On KEEP_VIDEO_FOR_PREVIEW=false the original is removed at the end of post_process.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from openai import AsyncOpenAI, APIConnectionError, AuthenticationError, RateLimitError

import database as db

logger = logging.getLogger(__name__)

CHUNK_DURATION = 600  # 10 minutes
KEEP_VIDEO = os.getenv("KEEP_VIDEO_FOR_PREVIEW", "true").lower() == "true"
RECAP_MODEL = os.getenv("RECAP_MODEL", "gpt-4o-mini")


async def get_duration(file_path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
        str(file_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    info = json.loads(stdout)
    return float(info["format"]["duration"])


async def _run_ffmpeg(args: list[str]) -> tuple[int, bytes]:
    """Run ffmpeg. If we're cancelled, terminate the subprocess cleanly so it
    doesn't keep running as an orphan and trashing its output file."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await proc.communicate()
        return proc.returncode, stderr
    except asyncio.CancelledError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        raise


async def enhance_video(input_path: Path, output_path: Path) -> tuple[bool, str | None]:
    """Produce a web-optimized proxy: cap resolution at 1080p (keeping aspect
    ratio), H.264 + faststart, enhanced with mild denoise/sharpen. Writes to a
    .tmp path and atomically renames on success so an interrupted ffmpeg can
    never leave a corrupt final file.

    Returns (ok, error). On failure, `error` is the tail of ffmpeg stderr
    (or the exception repr) so the caller can persist it for diagnosis.
    """
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.unlink(missing_ok=True)
    try:
        filtergraph = (
            "scale='if(gt(iw,1920),1920,iw)':'if(gt(ih,1080),1080,ih)':"
            "force_original_aspect_ratio=decrease:flags=lanczos,"
            "hqdn3d=3:2:3:2,"
            "unsharp=5:5:0.8:3:3:0.3,"
            "eq=brightness=0.03:contrast=1.03:saturation=1.08"
        )

        # Hardware-accelerated encoding first (Apple VideoToolbox — ~10-20x faster)
        rc_hw, stderr_hw = await _run_ffmpeg([
            "ffmpeg", "-i", str(input_path),
            "-vf", filtergraph,
            "-c:v", "h264_videotoolbox", "-q:v", "60",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            "-f", "mp4", str(tmp_path), "-y",
        ])
        rc, stderr = rc_hw, stderr_hw

        # Fall back to software encoding if hardware fails
        if rc_hw != 0:
            logger.info("Hardware encoding unavailable, falling back to software")
            tmp_path.unlink(missing_ok=True)
            rc_sw, stderr_sw = await _run_ffmpeg([
                "ffmpeg", "-i", str(input_path),
                "-vf", filtergraph,
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-maxrate", "4M", "-bufsize", "8M",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-pix_fmt", "yuv420p",
                "-f", "mp4", str(tmp_path), "-y",
            ])
            rc, stderr = rc_sw, stderr_sw

        if rc != 0:
            err = _format_ffmpeg_error(stderr, rc_hw, stderr_hw)
            logger.warning("Video enhancement failed: %s", err[-500:])
            tmp_path.unlink(missing_ok=True)
            return False, err

        if tmp_path.exists() and tmp_path.stat().st_size > 0:
            tmp_path.replace(output_path)
            return True, None
        else:
            tmp_path.unlink(missing_ok=True)
            return False, "ffmpeg reported success but produced no output file"

    except asyncio.CancelledError:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        logger.warning("Video enhancement failed (non-fatal): %s", e)
        tmp_path.unlink(missing_ok=True)
        return False, repr(e)


def _format_ffmpeg_error(final_stderr: bytes, rc_hw: int, stderr_hw: bytes) -> str:
    """Build a human-readable failure reason, preserving both attempts when the
    software fallback also failed. Truncated to 2 KB so we never blow up the row."""
    parts: list[str] = []
    if rc_hw != 0 and stderr_hw is not final_stderr:
        parts.append("[hardware (h264_videotoolbox)]")
        parts.append(stderr_hw.decode(errors="replace").strip())
        parts.append("\n[software (libx264)]")
    parts.append(final_stderr.decode(errors="replace").strip())
    text = "\n".join(parts)
    if len(text) > 2048:
        text = "…" + text[-2048:]
    return text


async def extract_audio(video_path: Path, output_path: Path) -> float:
    duration = await get_duration(video_path)

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-ab", "64k",
        "-f", "mp3", str(output_path), "-y",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode()}")

    return duration


async def split_audio(audio_path: Path, output_dir: Path) -> list[Path]:
    prefix = output_dir / f"{audio_path.stem}_chunk_"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", str(audio_path),
        "-f", "segment", "-segment_time", str(CHUNK_DURATION),
        "-c", "copy", f"{prefix}%03d.mp3",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg split failed: {stderr.decode()}")

    chunks = sorted(output_dir.glob(f"{audio_path.stem}_chunk_*.mp3"))
    return chunks


def format_timestamp_srt(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_timestamp_vtt(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def generate_srt(segments: list[dict]) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        start = format_timestamp_srt(seg["start"])
        end = format_timestamp_srt(seg["end"])
        speaker_prefix = f"[{seg['speaker']}] " if seg.get("speaker") else ""
        lines.append(f"{i}")
        lines.append(f"{start} --> {end}")
        lines.append(f"{speaker_prefix}{seg['text'].strip()}")
        lines.append("")
    return "\n".join(lines)


MAX_CUE_CHARS = 70   # target per-cue width so captions stay on one line
MAX_CUE_SECONDS = 4.0


def _split_for_captions(text: str, max_chars: int = MAX_CUE_CHARS) -> list[str]:
    """Break text into short word-aligned chunks for single-line caption display."""
    words = text.strip().split()
    if not words:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        w = len(word) + (1 if current else 0)
        if current_len + w > max_chars and current:
            chunks.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += w
    if current:
        chunks.append(" ".join(current))
    return chunks


def generate_vtt(segments: list[dict]) -> str:
    """Produce WebVTT with short cues (~70 chars, ~4s max each) so that caption
    display stays on a single line and never overtakes the video frame."""
    lines = ["WEBVTT", ""]
    for seg in segments:
        seg_start = float(seg.get("start") or 0)
        seg_end = float(seg.get("end") or seg_start)
        duration = max(seg_end - seg_start, 0.001)
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker_prefix = f"[{seg['speaker']}] " if seg.get("speaker") else ""
        chunks = _split_for_captions(text)
        if not chunks:
            continue
        # Apportion duration proportionally by chunk length, clamped so each
        # cue stays under MAX_CUE_SECONDS when possible.
        total_chars = sum(len(c) for c in chunks)
        t = seg_start
        for i, chunk in enumerate(chunks):
            share = len(chunk) / total_chars if total_chars else 1.0
            raw = duration * share
            chunk_dur = min(raw, MAX_CUE_SECONDS)
            chunk_end = min(t + chunk_dur, seg_end) if i < len(chunks) - 1 else seg_end
            if chunk_end <= t:
                chunk_end = t + 0.01
            prefix = speaker_prefix if i == 0 else ""
            # Pin cues to the very bottom of the video frame
            lines.append(f"{format_timestamp_vtt(t)} --> {format_timestamp_vtt(chunk_end)} line:88% position:50% align:middle")
            lines.append(f"{prefix}{chunk}")
            lines.append("")
            t = chunk_end
    return "\n".join(lines)


async def transcribe_file(client: AsyncOpenAI, file_path: Path) -> dict:
    with open(file_path, "rb") as f:
        response = await client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )
    return response.model_dump()


# ============================================================
# Speaker name identification via LLM
# ============================================================

async def identify_speaker_names(segments: list[dict], client: AsyncOpenAI) -> list[dict]:
    """Use GPT to identify real speaker names from transcript context. Raises on failure."""
    speakers = sorted({seg["speaker"] for seg in segments if seg.get("speaker")})
    if len(speakers) < 2:
        return segments

    sample_lines = []
    char_count = 0
    for seg in segments:
        if seg.get("speaker"):
            line = f"{seg['speaker']}: {seg['text'].strip()}"
            sample_lines.append(line)
            char_count += len(line)
            if char_count > 4000:
                break

    sample = "\n".join(sample_lines)

    response = await client.chat.completions.create(
        model=RECAP_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You analyze transcripts to identify speaker names. "
                    "Given a transcript with generic speaker labels (Speaker A, Speaker B, etc.), "
                    "determine the real names of each speaker from context clues in the conversation "
                    "(introductions, people addressing each other by name, etc.).\n\n"
                    "Return ONLY a JSON object mapping the original label to the identified name. "
                    "If you can identify a name, use it (first name only). "
                    "If you cannot confidently identify a name, assign a friendly distinguishing label "
                    "based on their role if apparent (e.g., 'Host', 'Interviewer', 'Presenter') "
                    "or keep the original label but make it friendlier (e.g., 'Speaker A' stays 'Speaker A').\n\n"
                    "Example response: {\"Speaker A\": \"Sarah\", \"Speaker B\": \"Mike\", \"Speaker C\": \"Speaker C\"}"
                ),
            },
            {
                "role": "user",
                "content": f"Speakers to identify: {', '.join(speakers)}\n\nTranscript:\n{sample}",
            },
        ],
        response_format={"type": "json_object"},
    )

    name_map = json.loads(response.choices[0].message.content)
    logger.info("Speaker name mapping for job: %s", name_map)

    for seg in segments:
        if seg.get("speaker") and seg["speaker"] in name_map:
            seg["speaker"] = name_map[seg["speaker"]]

    return segments


# ============================================================
# Auto recap generation
# ============================================================

async def generate_recap(plain_text: str, client: AsyncOpenAI, guidance: str | None = None) -> str:
    """Generate a meeting recap email from transcript text. Raises on API failure.

    `guidance` is optional free-form instruction from the user (e.g. "make it
    shorter", "address it to Pete", "focus on action items") that is layered on
    top of the default system prompt when the user regenerates the recap.
    """
    transcript = plain_text
    if len(transcript) > 60000:
        transcript = transcript[:60000] + "\n\n[...transcript truncated for length]"

    guidance = (guidance or "").strip()
    user_content = f"Here is the transcript:\n\n{transcript}"
    if guidance:
        # Hard-cap to avoid accidental prompt bloat from pasted content.
        if len(guidance) > 2000:
            guidance = guidance[:2000]
        user_content = (
            f"Additional instructions from the user for this regeneration "
            f"(follow these carefully, but do not violate the quality rules "
            f"in the system message):\n{guidance}\n\n"
            f"Here is the transcript:\n\n{transcript}"
        )

    response = await client.chat.completions.create(
        model=RECAP_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are summarizing a meeting transcript. Produce a clean, professional "
                    "summary in the following format. Do not include pleasantries, filler, or "
                    "motivational closings.\n\n"
                    "SUBJECT: [One-line description of the meeting's purpose]\n\n"
                    "SUMMARY\n"
                    "Two to four sentences describing what was actually covered and why it "
                    "mattered. Be specific about subject matter, not just topic labels. Plain "
                    "language, no adjectives like \"productive\" or \"great.\"\n\n"
                    "KEY POINTS\n"
                    "- Bullets describing substantive content discussed, not just topic "
                    "headings. Each bullet should convey what was said or concluded about that "
                    "topic, not merely that it was mentioned.\n"
                    "- If a topic was only briefly touched on, say so.\n\n"
                    "DECISIONS\n"
                    "- Only list items that were actually decided — a choice made between "
                    "options, a policy adopted, an approach agreed on.\n"
                    "- Do NOT list scheduled follow-ups, assigned tasks, or things mentioned in "
                    "passing. Those belong in Next Steps or are omitted.\n"
                    "- If no decisions were made, write \"None.\"\n\n"
                    "NEXT STEPS\n"
                    "- Each item must have a named owner (use actual names from the transcript) "
                    "and a specific, actionable deliverable.\n"
                    "- Include a due date or timeframe if one was stated.\n"
                    "- Do NOT include vague directives like \"review the material\" unless they "
                    "were explicitly assigned.\n"
                    "- If no next steps were assigned, write \"None.\"\n\n"
                    "Rules:\n"
                    "- Never leave placeholder text like [Owner] or [Your Name]. If you don't "
                    "know a name, omit the bullet rather than use a placeholder.\n"
                    "- No opening greetings, sign-offs, or thank-yous.\n"
                    "- Use plain, direct language. Avoid exclamations, emojis, and phrases like "
                    "\"dived into,\" \"unpacked,\" or \"let's keep the momentum going.\"\n"
                    "- Keep the whole summary under 250 words unless the meeting genuinely "
                    "requires more."
                ),
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
    )
    return response.choices[0].message.content or ""


# ============================================================
# Post-processing: speaker names + recap (runs after transcription)
# ============================================================

async def _tick_progress(job_id: str, start_pct: int, end_pct: int, estimated_s: float):
    """Tick `progress` from start_pct -> (end_pct - 1) smoothly over `estimated_s`.
    Runs until cancelled; intermediate per-batch updates will override as they land."""
    import time as _time
    started = _time.monotonic()
    span = max(1, end_pct - 1 - start_pct)
    try:
        while True:
            elapsed = _time.monotonic() - started
            frac = min(elapsed / max(estimated_s, 1.0), 1.0)
            pct = start_pct + int(frac * span)
            if pct > end_pct - 1:
                pct = end_pct - 1
            await db.update_transcription(job_id, progress=pct)
            await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        pass


async def post_process(job_id: str, all_segments: list[dict], plain_text: str,
                       video_path: Path | None = None):
    """Speaker names, recap, and video enhancement — parallel, non-fatal. Records status per subtask."""
    client = AsyncOpenAI()
    has_speakers = any(seg.get("speaker") for seg in all_segments)
    should_enhance = bool(video_path and video_path.exists() and KEEP_VIDEO)

    tasks = [
        identify_speaker_names(all_segments, client) if has_speakers else _passthrough(all_segments),
        generate_recap(plain_text, client),
    ]
    if should_enhance:
        enhanced_path = video_path.parent / f"{video_path.stem}_enhanced.mp4"
        tasks.append(enhance_video(video_path, enhanced_path))
    else:
        tasks.append(_passthrough(None))

    speaker_res, recap_res, enhance_res = await asyncio.gather(*tasks, return_exceptions=True)

    updates: dict = {}

    # Speaker identification
    if not has_speakers:
        updates["speaker_id_status"] = "skipped"
    elif isinstance(speaker_res, Exception):
        logger.warning("Speaker identification failed (non-fatal): %s", speaker_res)
        updates["speaker_id_status"] = "failed"
    else:
        updates["speaker_id_status"] = "ok"
        updated_plain = " ".join(seg["text"].strip() for seg in speaker_res)
        updates["transcript_text"] = updated_plain
        updates["transcript_srt"] = generate_srt(speaker_res)
        updates["transcript_vtt"] = generate_vtt(speaker_res)
        updates["transcript_segments_json"] = json.dumps(speaker_res)

    # Recap (always attempted when post_process runs)
    if isinstance(recap_res, Exception):
        logger.warning("Recap generation failed (non-fatal): %s", recap_res)
        updates["recap_status"] = "failed"
    elif recap_res:
        updates["recap"] = recap_res
        updates["recap_status"] = "ok"
    else:
        updates["recap_status"] = "failed"

    # Video enhancement
    if not should_enhance:
        updates["enhancement_status"] = "skipped"
    elif isinstance(enhance_res, Exception):
        logger.warning("Video enhancement crashed (non-fatal): %s", enhance_res)
        updates["enhancement_status"] = "failed"
        updates["enhancement_error"] = repr(enhance_res)
        updates["video_path"] = str(video_path)
    else:
        ok, reason = enhance_res
        if ok:
            enhanced_path = video_path.parent / f"{video_path.stem}_enhanced.mp4"
            video_path.unlink(missing_ok=True)
            updates["video_path"] = str(enhanced_path)
            updates["enhancement_status"] = "ok"
            updates["enhancement_error"] = None
        else:
            updates["enhancement_status"] = "failed"
            updates["enhancement_error"] = reason
            updates["video_path"] = str(video_path)

    await db.update_transcription(job_id, **updates)

    # Index the transcript for the AI Assistant (non-fatal). Uses the final
    # segments after speaker-id if available, otherwise the originals.
    try:
        final_segments_json = updates.get("transcript_segments_json")
        if not final_segments_json:
            final_segments_json = json.dumps(all_segments)
        record = await db.get_transcription(job_id)
        owner = record.get("user_id") if record else None
        if owner:
            from retrieval import embed_and_store_transcription
            await embed_and_store_transcription(
                transcription_id=job_id,
                user_id=owner,
                segments_json=final_segments_json,
                transcript_text=updates.get("transcript_text") or plain_text,
            )
    except Exception as e:
        logger.warning("AI indexing failed for %s (non-fatal): %s", job_id, e)


async def _passthrough(val):
    return val


def _user_friendly_error(exc: Exception) -> str:
    if isinstance(exc, RateLimitError):
        return "OpenAI rate limit reached. Wait a few minutes and retry, or check your API plan limits."
    if isinstance(exc, AuthenticationError):
        return "OpenAI authentication failed. Check that OPENAI_API_KEY is valid."
    if isinstance(exc, APIConnectionError):
        return "Could not reach OpenAI. Check your network connection and try again."
    return str(exc)


# ============================================================
# Main transcription pipelines
# ============================================================

async def process_transcription(job_id: str, video_path: Path, audio_dir: Path, diarize: bool = False):
    # Route to AssemblyAI pipeline if diarization requested
    assemblyai_key = os.getenv("ASSEMBLYAI_API_KEY")
    if diarize and assemblyai_key:
        return await process_transcription_assemblyai(job_id, video_path, audio_dir, assemblyai_key)

    client = AsyncOpenAI()
    audio_path = audio_dir / f"{job_id}.mp3"

    try:
        # Stage 1: Extract audio
        await db.update_transcription(job_id, status="extracting", progress=5)
        duration = await extract_audio(video_path, audio_path)
        await db.update_transcription(job_id, duration_seconds=duration, progress=10)

        if audio_path.stat().st_size == 0:
            raise RuntimeError("No audio track found in the video file")

        # Stage 2: Determine if chunking is needed
        now = datetime.now(timezone.utc).isoformat()
        await db.update_transcription(
            job_id, status="transcribing", progress=15,
            processing_started_at=now,
        )

        file_size_mb = audio_path.stat().st_size / (1024 * 1024)
        if file_size_mb > 24:
            chunks = await split_audio(audio_path, audio_dir)
        else:
            chunks = [audio_path]

        total_chunks = len(chunks)
        await db.update_transcription(
            job_id, total_chunks=total_chunks, completed_chunks=0,
        )

        # Stage 3: Transcribe chunks in parallel (batches of 8).
        # Run a progress ticker alongside so single-chunk files (and the time
        # between batch completions) don't stall at a fixed percentage.
        all_segments = []
        PARALLEL_BATCH = 8
        completed = 0
        whisper_estimated_s = max(20.0, (duration or 60.0) * 0.20)
        stage_tick = asyncio.create_task(_tick_progress(job_id, 15, 92, whisper_estimated_s))
        try:
            for batch_start in range(0, total_chunks, PARALLEL_BATCH):
                batch = list(enumerate(chunks[batch_start:batch_start + PARALLEL_BATCH], start=batch_start))

                async def transcribe_chunk(idx, path):
                    chunk_offset = idx * CHUNK_DURATION if total_chunks > 1 else 0
                    result = await transcribe_file(client, path)
                    segs = []
                    for seg in result.get("segments", []):
                        segs.append({
                            "start": seg["start"] + chunk_offset,
                            "end": seg["end"] + chunk_offset,
                            "text": seg["text"],
                        })
                    return idx, segs

                results = await asyncio.gather(*(transcribe_chunk(idx, path) for idx, path in batch))

                for idx, segs in sorted(results, key=lambda x: x[0]):
                    all_segments.extend(segs)

                completed += len(batch)
                progress = 15 + int(completed / total_chunks * 80)
                await db.update_transcription(
                    job_id, progress=progress, completed_chunks=completed,
                )
        finally:
            stage_tick.cancel()

        # Stage 4: Generate output formats
        plain_text = " ".join(seg["text"].strip() for seg in all_segments)
        srt_text = generate_srt(all_segments)
        vtt_text = generate_vtt(all_segments)
        segments_json = json.dumps(all_segments)

        await db.update_transcription(
            job_id,
            status="done",
            progress=100,
            transcript_text=plain_text,
            transcript_srt=srt_text,
            transcript_vtt=vtt_text,
            transcript_segments_json=segments_json,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

        # Stage 5: Post-process (speaker names + recap + video enhance) — all in parallel, non-fatal
        await post_process(job_id, all_segments, plain_text, video_path=video_path)

    except Exception as e:
        logger.exception("Transcription failed for job %s", job_id)
        await db.update_transcription(
            job_id,
            status="error",
            error_message=_user_friendly_error(e),
            video_path=str(video_path) if video_path.exists() else None,
        )

    finally:
        audio_path.unlink(missing_ok=True)
        for chunk in audio_dir.glob(f"{job_id}_chunk_*.mp3"):
            chunk.unlink(missing_ok=True)
        # On success, post_process owns the video (moves/removes as needed).
        # On error, the original video stays and video_path was set in the
        # except block above, so /api/transcriptions/{id}/retry can find it.


async def process_transcription_assemblyai(
    job_id: str, video_path: Path, audio_dir: Path, api_key: str
):
    import assemblyai as aai

    aai.settings.api_key = api_key
    audio_path = audio_dir / f"{job_id}.mp3"

    try:
        # Stage 1: Extract audio
        await db.update_transcription(job_id, status="extracting", progress=5)
        duration = await extract_audio(video_path, audio_path)
        await db.update_transcription(job_id, duration_seconds=duration, progress=10)

        if audio_path.stat().st_size == 0:
            raise RuntimeError("No audio track found in the video file")

        # Stage 2: Submit to AssemblyAI with speaker diarization
        now = datetime.now(timezone.utc).isoformat()
        await db.update_transcription(
            job_id, status="transcribing", progress=15,
            processing_started_at=now, total_chunks=1, completed_chunks=0,
        )

        config = aai.TranscriptionConfig(
            speaker_labels=True,
            speech_models=[aai.SpeechModel.universal],
        )
        transcriber = aai.Transcriber()

        # Run the blocking transcribe call on a thread and tick progress smoothly
        # until it finishes. Estimate ~25% of audio duration for the API.
        estimated_s = max(30.0, (duration or 60.0) * 0.25)
        tick_task = asyncio.create_task(_tick_progress(job_id, 15, 82, estimated_s))
        try:
            transcript = await asyncio.to_thread(
                transcriber.transcribe, str(audio_path), config=config
            )
        finally:
            tick_task.cancel()

        if transcript.status == aai.TranscriptStatus.error:
            raise RuntimeError(f"AssemblyAI error: {transcript.error}")

        await db.update_transcription(job_id, progress=85, completed_chunks=1)

        # Stage 3: Convert AssemblyAI utterances to our segment format
        all_segments = []

        if transcript.utterances:
            for utt in transcript.utterances:
                all_segments.append({
                    "start": utt.start / 1000.0,
                    "end": utt.end / 1000.0,
                    "text": utt.text,
                    "speaker": f"Speaker {utt.speaker}",
                })
        elif transcript.words:
            current_seg = None
            for word in transcript.words:
                if current_seg is None or (word.start / 1000.0 - current_seg["end"]) > 1.0:
                    if current_seg:
                        all_segments.append(current_seg)
                    current_seg = {
                        "start": word.start / 1000.0,
                        "end": word.end / 1000.0,
                        "text": word.text,
                        "speaker": f"Speaker {word.speaker}" if hasattr(word, "speaker") and word.speaker else None,
                    }
                else:
                    current_seg["end"] = word.end / 1000.0
                    current_seg["text"] += f" {word.text}"
            if current_seg:
                all_segments.append(current_seg)

        # Stage 4: Generate output formats
        plain_text = " ".join(seg["text"].strip() for seg in all_segments)
        srt_text = generate_srt(all_segments)
        vtt_text = generate_vtt(all_segments)
        segments_json = json.dumps(all_segments)

        await db.update_transcription(
            job_id,
            status="done",
            progress=100,
            transcript_text=plain_text,
            transcript_srt=srt_text,
            transcript_vtt=vtt_text,
            transcript_segments_json=segments_json,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

        # Stage 5: Post-process (speaker names + recap + video enhance) — all in parallel
        await post_process(job_id, all_segments, plain_text, video_path=video_path)

    except Exception as e:
        logger.exception("AssemblyAI transcription failed for job %s", job_id)
        await db.update_transcription(
            job_id,
            status="error",
            error_message=_user_friendly_error(e),
            video_path=str(video_path) if video_path.exists() else None,
        )

    finally:
        audio_path.unlink(missing_ok=True)
        for chunk in audio_dir.glob(f"{job_id}_chunk_*.mp3"):
            chunk.unlink(missing_ok=True)
        # Same lifecycle as the Whisper path above.
