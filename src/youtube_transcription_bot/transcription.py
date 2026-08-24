"""Bounded AssemblyAI transcription for local files and validated URLs."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import subprocess
import time
from pathlib import Path

import assemblyai as aai

from .config import Settings
from .errors import MediaLimitError, TranscriptUnavailableError

logger = logging.getLogger(__name__)
ASSEMBLYAI_MODEL = "universal-2"
ASSEMBLYAI_TIMEOUT_SECONDS = 3 * 60 * 60
ASSEMBLYAI_POLL_SECONDS = 3.0
ASSEMBLYAI_MAX_POLL_ERRORS = 5
FFMPEG_TIMEOUT_SECONDS = 10 * 60
FFPROBE_TIMEOUT_SECONDS = 2 * 60


class AssemblyAITranscriber:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _transcribe_blocking(self, source: str) -> str:
        aai.settings.api_key = self._settings.assemblyai_api_key.get_secret_value()
        config = aai.TranscriptionConfig(speech_models=[ASSEMBLYAI_MODEL])
        transcript = aai.Transcriber().submit(source, config=config)
        transcript_id = transcript.id
        if not transcript_id:
            raise TranscriptUnavailableError(
                "AssemblyAI did not accept the transcription job."
            )

        deadline = time.monotonic() + ASSEMBLYAI_TIMEOUT_SECONDS
        consecutive_errors = 0
        complete_statuses = {
            aai.TranscriptStatus.completed,
            aai.TranscriptStatus.error,
        }
        while transcript.status not in complete_statuses:
            if time.monotonic() >= deadline:
                raise TranscriptUnavailableError("AssemblyAI transcription timed out.")
            time.sleep(ASSEMBLYAI_POLL_SECONDS)
            try:
                transcript = aai.Transcript.get_by_id(transcript_id)
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                if consecutive_errors >= ASSEMBLYAI_MAX_POLL_ERRORS:
                    raise TranscriptUnavailableError(
                        "AssemblyAI could not be reached while polling the job."
                    ) from exc

        if transcript.status == aai.TranscriptStatus.error:
            raise TranscriptUnavailableError(
                "AssemblyAI could not transcribe this media."
            )
        text = transcript.text
        if not isinstance(text, str) or not text.strip():
            raise TranscriptUnavailableError("AssemblyAI returned an empty transcript.")
        return text.strip()

    async def transcribe_file(self, path: Path) -> str:
        if not path.is_file():
            raise TranscriptUnavailableError("The temporary media file is unavailable.")
        if path.stat().st_size > self._settings.max_attachment_bytes:
            raise MediaLimitError(
                "The uploaded media exceeds the configured size limit."
            )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._transcribe_blocking, str(path)),
                timeout=ASSEMBLYAI_TIMEOUT_SECONDS + 60,
            )
        except TimeoutError as exc:
            raise TranscriptUnavailableError(
                "AssemblyAI transcription timed out."
            ) from exc

    async def transcribe_url(self, url: str) -> str:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._transcribe_blocking, url),
                timeout=ASSEMBLYAI_TIMEOUT_SECONDS + 60,
            )
        except TimeoutError as exc:
            raise TranscriptUnavailableError(
                "AssemblyAI transcription timed out."
            ) from exc


async def validate_local_media_duration(path: Path, settings: Settings) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]

    def run() -> float:
        try:
            result = subprocess.run(  # noqa: S603 - fixed executable and no shell
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=FFPROBE_TIMEOUT_SECONDS,
                env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise TranscriptUnavailableError(
                "The uploaded media duration could not be inspected."
            ) from exc
        try:
            duration = float(result.stdout.strip())
        except ValueError as exc:
            raise TranscriptUnavailableError(
                "The uploaded media duration could not be inspected."
            ) from exc
        if result.returncode != 0 or not math.isfinite(duration) or duration <= 0:
            raise TranscriptUnavailableError(
                "The uploaded media duration could not be inspected."
            )
        return duration

    duration = await asyncio.to_thread(run)
    if duration > settings.max_media_duration_seconds:
        raise MediaLimitError("The uploaded media exceeds the duration limit.")
    return duration


async def extract_audio_from_video(
    source: Path,
    destination: Path,
    settings: Settings,
) -> Path:
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(destination),
    ]

    def run() -> None:
        try:
            subprocess.run(  # noqa: S603 - fixed executable and no shell
                command,
                check=True,
                capture_output=True,
                timeout=FFMPEG_TIMEOUT_SECONDS,
                env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise TranscriptUnavailableError(
                "The uploaded video audio could not be extracted."
            ) from exc

    await asyncio.to_thread(run)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise TranscriptUnavailableError(
            "The uploaded video contained no usable audio."
        )
    if destination.stat().st_size > settings.max_attachment_bytes:
        raise MediaLimitError("The extracted audio exceeds the configured size limit.")
    return destination
