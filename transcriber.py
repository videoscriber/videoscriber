import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from openai import AsyncOpenAI

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


async def enhance_video(input_path: Path, output_path: Path) -> bool:
    """Enhance video quality. Writes to a .tmp path and atomically renames on
    success so an interrupted ffmpeg can never leave a corrupt final file."""
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.unlink(missing_ok=True)
    try:
        filtergraph = (
            "hqdn3d=3:2:3:2,"
            "unsharp=5:5:0.8:3:3:0.3,"
            "eq=brightness=0.03:contrast=1.03:saturation=1.08"
        )

        # Hardware-accelerated encoding first (Apple VideoToolbox — ~10-20x faster)
        rc, stderr = await _run_ffmpeg([
            "ffmpeg", "-i", str(input_path),
            "-vf", filtergraph,
            "-c:v", "h264_videotoolbox", "-q:v", "65",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(tmp_path), "-y",
        ])

        # Fall back to software encoding if hardware fails
        if rc != 0:
            logger.info("Hardware encoding unavailable, falling back to software")
            tmp_path.unlink(missing_ok=True)
            rc, stderr = await _run_ffmpeg([
                "ffmpeg", "-i", str(input_path),
                "-vf", filtergraph,
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(tmp_path), "-y",
            ])

        if rc != 0:
            logger.warning("Video enhancement failed: %s", stderr.decode()[-500:])
            tmp_path.unlink(missing_ok=True)
            return False

        if tmp_path.exists() and tmp_path.stat().st_size > 0:
            # Atomic rename — the final file only ever appears when ffmpeg
            # finished writing and the moov atom is in place.
            tmp_path.replace(output_path)
            return True
        else:
            tmp_path.unlink(missing_ok=True)
            return False

    except asyncio.CancelledError:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        logger.warning("Video enhancement failed (non-fatal): %s", e)
        tmp_path.unlink(missing_ok=True)
        return False


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


def generate_vtt(segments: list[dict]) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        start = format_timestamp_vtt(seg["start"])
        end = format_timestamp_vtt(seg["end"])
        speaker_prefix = f"[{seg['speaker']}] " if seg.get("speaker") else ""
        lines.append(f"{start} --> {end}")
        lines.append(f"{speaker_prefix}{seg['text'].strip()}")
        lines.append("")
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

async def generate_recap(plain_text: str, client: AsyncOpenAI) -> str:
    """Generate a meeting recap email from transcript text. Raises on API failure."""
    transcript = plain_text
    if len(transcript) > 60000:
        transcript = transcript[:60000] + "\n\n[...transcript truncated for length]"

    response = await client.chat.completions.create(
        model=RECAP_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are the ideal coworker — sharp, warm, and genuinely helpful. "
                    "You write recap emails that people actually enjoy reading. "
                    "Your tone is professional but human: a touch of wit when appropriate, "
                    "always clear, never stuffy or robotic.\n\n"
                    "Given a meeting transcript, write a recap email in PLAIN TEXT (no markdown, "
                    "no asterisks, no bold formatting — this will be pasted into an email client).\n\n"
                    "Format:\n\n"
                    "Subject: [One clear line suitable as an email subject]\n\n"
                    "Hey team,\n\n"
                    "[2-3 sentence summary that captures the vibe and substance of the meeting]\n\n"
                    "KEY POINTS\n"
                    "- [bullet points of main topics discussed]\n\n"
                    "DECISIONS\n"
                    "- [bullet points — omit this section entirely if no clear decisions were made]\n\n"
                    "NEXT STEPS\n"
                    "- [Owner]: [action item] — [deadline if mentioned]\n"
                    "- [bullet points with names attached when identifiable]\n\n"
                    "[Brief, friendly sign-off — keep it natural, like a real person wrote it]\n\n"
                    "Rules:\n"
                    "- Use real names from the transcript when you can identify them\n"
                    "- No asterisks, no markdown, no bold/italic formatting\n"
                    "- Use CAPS for section headers (KEY POINTS, DECISIONS, NEXT STEPS)\n"
                    "- Keep it concise — respect people's inboxes\n"
                    "- A touch of personality is welcome but don't force humor"
                ),
            },
            {
                "role": "user",
                "content": f"Here is the transcript:\n\n{transcript}",
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
        updates["video_path"] = str(video_path)
    elif enhance_res:
        enhanced_path = video_path.parent / f"{video_path.stem}_enhanced.mp4"
        video_path.unlink(missing_ok=True)
        updates["video_path"] = str(enhanced_path)
        updates["enhancement_status"] = "ok"
    else:
        updates["enhancement_status"] = "failed"
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
    success = False

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
        success = True

        # Stage 5: Post-process (speaker names + recap + video enhance) — all in parallel, non-fatal
        await post_process(job_id, all_segments, plain_text, video_path=video_path)

    except Exception as e:
        logger.exception("Transcription failed for job %s", job_id)
        await db.update_transcription(
            job_id,
            status="error",
            error_message=str(e),
            video_path=str(video_path) if video_path.exists() else None,
        )

    finally:
        audio_path.unlink(missing_ok=True)
        for chunk in audio_dir.glob(f"{job_id}_chunk_*.mp3"):
            chunk.unlink(missing_ok=True)
        # Video cleanup handled by post_process; on error keep for retry
        if not success and video_path.exists():
            pass  # kept for retry — video_path stored in error handler


async def process_transcription_assemblyai(
    job_id: str, video_path: Path, audio_dir: Path, api_key: str
):
    import assemblyai as aai

    aai.settings.api_key = api_key
    audio_path = audio_dir / f"{job_id}.mp3"
    success = False

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
        success = True

        # Stage 5: Post-process (speaker names + recap + video enhance) — all in parallel
        await post_process(job_id, all_segments, plain_text, video_path=video_path)

    except Exception as e:
        logger.exception("AssemblyAI transcription failed for job %s", job_id)
        await db.update_transcription(
            job_id,
            status="error",
            error_message=str(e),
            video_path=str(video_path) if video_path.exists() else None,
        )

    finally:
        audio_path.unlink(missing_ok=True)
        for chunk in audio_dir.glob(f"{job_id}_chunk_*.mp3"):
            chunk.unlink(missing_ok=True)
        if not success and video_path.exists():
            pass  # kept for retry
