from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import media_transcription_bot.youtube as youtube_module
from media_transcription_bot.errors import (
    TranscriptUnavailableError,
    UnsupportedInputError,
)
from media_transcription_bot.youtube import (
    YOUTUBE_TRANSCRIPT_REQUEST_TIMEOUT_SECONDS,
    VideoMetadata,
    YouTubeService,
    _TimeoutSession,
    extract_youtube_id,
    normalize_caption_lines,
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


def test_caption_normalization_removes_shared_word_overlap_and_repeated_windows():
    repeated = "one two three four five six " * 3
    assert normalize_caption_lines(
        [
            "The speaker explains the first important point",
            "the first important point and then gives evidence",
            f"{repeated}closing thought",
        ]
    ) == (
        "The speaker explains the first important point and then gives evidence "
        "one two three four five six closing thought"
    )


def test_timeout_session_applies_default_transport_timeout(monkeypatch):
    captured: dict[str, object] = {}

    def request(self, method, url, *args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(requests.Session, "request", request)
    session = _TimeoutSession(YOUTUBE_TRANSCRIPT_REQUEST_TIMEOUT_SECONDS)

    session.request("GET", "https://www.youtube.com/")

    assert captured["timeout"] == YOUTUBE_TRANSCRIPT_REQUEST_TIMEOUT_SECONDS


def test_timed_transcript_wires_timeout_session_and_headers(
    monkeypatch,
    settings_factory,
):
    captured: dict[str, object] = {}

    class FakeTranscriptApi:
        def __init__(self, *, proxy_config=None, http_client=None):
            captured["proxy_config"] = proxy_config
            captured["http_client"] = http_client

        def fetch(self, video_id, *, languages):
            assert video_id == "dQw4w9WgXcQ"
            assert languages == ["en"]
            return [
                SimpleNamespace(text="second caption", start=10.0),
                SimpleNamespace(text="first caption", start=0.0),
            ]

    monkeypatch.setattr(youtube_module, "YouTubeTranscriptApi", FakeTranscriptApi)
    settings = settings_factory()
    service = YouTubeService(settings, SimpleNamespace())
    identity = youtube_module.build_youtube_identity(settings)

    assert service._timed_transcript("dQw4w9WgXcQ", identity) == (
        "first caption second caption"
    )
    http_client = captured["http_client"]
    assert isinstance(http_client, _TimeoutSession)
    assert http_client.headers["User-Agent"] == identity.user_agent
    assert http_client.headers["Accept-Language"] == identity.accept_language


def test_ytdlp_metadata_cookie_policy_matches_fallback_profile(
    monkeypatch,
    settings_factory,
    tmp_path,
):
    cookies = tmp_path / "runtime-state.txt"
    cookies.write_text("not-real", encoding="utf-8")
    settings = settings_factory(youtube_cookies_file=cookies)
    service = YouTubeService(settings, SimpleNamespace())
    identity = youtube_module.build_youtube_identity(settings)
    commands: list[list[str]] = []

    def run(command, identity, *, timeout):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout='{"title":"Example","duration":60}',
            stderr="",
        )

    monkeypatch.setattr(youtube_module, "_run_command", run)

    service._metadata_with_ytdlp(
        "dQw4w9WgXcQ",
        identity,
        use_cookies=False,
    )
    service._metadata_with_ytdlp(
        "dQw4w9WgXcQ",
        identity,
        use_cookies=True,
    )

    assert "--cookies" not in commands[0]
    assert commands[1][commands[1].index("--cookies") + 1] == str(cookies)


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

    def ytdlp_metadata(video_id, identity, *, use_cookies):
        events.append("media_metadata")
        assert use_cookies is True
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
