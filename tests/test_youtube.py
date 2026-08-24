from __future__ import annotations

from pathlib import Path

import pytest

from youtube_transcription_bot.errors import (
    TranscriptUnavailableError,
    UnsupportedInputError,
)
from youtube_transcription_bot.youtube import (
    VideoMetadata,
    YouTubeService,
    extract_youtube_id,
    parse_vtt_transcript,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=4", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ],
)
def test_extract_youtube_id(url, expected):
    assert extract_youtube_id(url) == expected


def test_extract_youtube_id_rejects_lookalike_host():
    with pytest.raises(UnsupportedInputError):
        extract_youtube_id("https://youtube.com.example/watch?v=dQw4w9WgXcQ")


def test_vtt_parser_removes_timestamps_tags_and_cumulative_duplicates():
    value = """WEBVTT

00:00:00.000 --> 00:00:02.000
<c>Hello world</c>

00:00:02.000 --> 00:00:04.000
Hello world from the speaker

00:00:04.000 --> 00:00:06.000
Second thought
"""
    assert parse_vtt_transcript(value) == (
        "Hello world from the speaker Second thought"
    )


@pytest.mark.asyncio
async def test_youtube_fallback_order_reaches_assemblyai(
    monkeypatch,
    settings_factory,
):
    events: list[str] = []

    class FakeAssemblyAI:
        async def transcribe_file(self, path: Path) -> str:
            events.append("assemblyai")
            assert path.read_bytes() == b"audio"
            return "fallback transcript"

    service = YouTubeService(settings_factory(), FakeAssemblyAI())

    def metadata(video_id, identity):
        events.append("oembed")
        return VideoMetadata("Example", "Channel", None)

    def timed(video_id, identity):
        events.append("timed_transcript")
        raise RuntimeError("unavailable")

    def subtitles(video_id, identity, temp_dir):
        events.append("subtitles")
        raise TranscriptUnavailableError("unavailable")

    def ytdlp_metadata(video_id, identity):
        events.append("media_metadata")
        return VideoMetadata("Example", "Channel", 60)

    def download(video_id, identity, temp_dir, **kwargs):
        events.append("audio_download")
        output = temp_dir / "audio.webm"
        output.write_bytes(b"audio")
        return output

    monkeypatch.setattr(service, "_oembed_metadata", metadata)
    monkeypatch.setattr(service, "_timed_transcript", timed)
    monkeypatch.setattr(service, "_subtitle_transcript", subtitles)
    monkeypatch.setattr(service, "_metadata_with_ytdlp", ytdlp_metadata)
    monkeypatch.setattr(service, "_download_audio", download)

    artifact = await service.transcribe("https://youtu.be/dQw4w9WgXcQ")

    assert artifact.transcript == "fallback transcript"
    assert artifact.transcript_source == "assemblyai"
    assert events == [
        "oembed",
        "timed_transcript",
        "subtitles",
        "media_metadata",
        "audio_download",
        "assemblyai",
    ]
