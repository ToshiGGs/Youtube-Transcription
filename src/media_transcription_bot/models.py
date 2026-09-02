"""Typed values shared across ingestion paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

TranscriptSource = Literal["youtube", "youtube_subtitles", "assemblyai"]


@dataclass(frozen=True)
class TranscriptArtifact:
    title: str
    source_url: str | None
    transcript: str
    transcript_source: TranscriptSource
    summary_context: str
    filename_stem: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessedArtifact:
    transcript: TranscriptArtifact
    summary: str


@dataclass(frozen=True)
class PodcastEpisode:
    title: str
    feed_url: str
    enclosure_url: str
    feed_title: str | None = None
    link: str | None = None
    guid: str | None = None
    published_at: datetime | None = None
    duration_seconds: float | None = None
    enclosure_type: str | None = None
    enclosure_length: int | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class RemoteMedia:
    final_url: str
    content_type: str | None
    content_length: int | None
