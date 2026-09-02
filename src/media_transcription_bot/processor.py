"""One orchestration surface for every Discord ingestion path."""

from __future__ import annotations

import re
from pathlib import Path

from .config import Settings
from .models import ProcessedArtifact, TranscriptArtifact
from .podcast import PodcastService
from .summary import SummaryService
from .transcription import (
    AssemblyAITranscriber,
    extract_audio_from_video,
    validate_local_media_duration,
)
from .youtube import YouTubeService


def safe_filename_stem(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return normalized.strip("-._")[:100] or "media"


class MediaProcessor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._assemblyai = AssemblyAITranscriber(settings)
        self._youtube = YouTubeService(settings, self._assemblyai)
        self._podcast = PodcastService(settings, self._assemblyai)
        self._summary = SummaryService(settings)

    async def close(self) -> None:
        await self._summary.close()

    async def _finish(self, transcript: TranscriptArtifact) -> ProcessedArtifact:
        summary = await self._summary.summarize(
            transcript.transcript,
            transcript.summary_context,
        )
        return ProcessedArtifact(transcript=transcript, summary=summary)

    async def process_youtube(self, value: str) -> ProcessedArtifact:
        return await self._finish(await self._youtube.transcribe(value))

    async def process_podcast(self, value: str) -> ProcessedArtifact:
        return await self._finish(await self._podcast.transcribe(value))

    async def process_attachment(
        self,
        path: Path,
        *,
        filename: str,
        content_type: str,
    ) -> ProcessedArtifact:
        await validate_local_media_duration(path, self._settings)
        transcription_path = path
        extracted_path: Path | None = None
        if content_type.lower().startswith("video/"):
            extracted_path = path.with_name("extracted-audio.m4a")
            transcription_path = await extract_audio_from_video(
                path,
                extracted_path,
                self._settings,
            )
        try:
            transcript_text = await self._assemblyai.transcribe_file(transcription_path)
        finally:
            if extracted_path is not None:
                extracted_path.unlink(missing_ok=True)
        safe_name = Path(filename.replace("\\", "/")).name or "attachment"
        artifact = TranscriptArtifact(
            title=safe_name,
            source_url=None,
            transcript=transcript_text,
            transcript_source="assemblyai",
            summary_context=f"Uploaded media filename: {safe_name}",
            filename_stem=f"upload-{safe_filename_stem(safe_name)}",
            metadata={"Content type": content_type},
        )
        return await self._finish(artifact)
