from __future__ import annotations

from types import SimpleNamespace

import pytest

import youtube_transcription_bot.bot as bot_module
from youtube_transcription_bot.bot import (
    JobKind,
    _download_attachment,
    _write_bounded_chunks,
    route_message,
)
from youtube_transcription_bot.errors import MediaLimitError, UnsupportedInputError


def _message(content: str, attachments=None):
    return SimpleNamespace(content=content, attachments=attachments or [])


def test_pasted_youtube_url_routes_automatically():
    job = route_message(_message("watch https://youtu.be/dQw4w9WgXcQ"))
    assert job is not None
    assert job.kind == JobKind.YOUTUBE


def test_known_podcast_url_routes_automatically():
    job = route_message(_message("https://open.spotify.com/episode/1234567890abcdef"))
    assert job is not None
    assert job.kind == JobKind.PODCAST


def test_generic_url_requires_podcast_command():
    assert route_message(_message("https://example.com/feed.xml")) is None
    job = route_message(_message("!podcast https://example.com/feed.xml"))
    assert job is not None
    assert job.kind == JobKind.PODCAST


@pytest.mark.asyncio
async def test_attachment_stream_stops_before_writing_over_limit(tmp_path):
    async def chunks():
        yield b"1234"
        yield b"5678"

    path = tmp_path / "attachment.media"
    with pytest.raises(MediaLimitError, match="size limit"):
        await _write_bounded_chunks(chunks(), path, max_bytes=5)

    assert path.read_bytes() == b"1234"


@pytest.mark.asyncio
async def test_attachment_stream_offloads_disk_writes(monkeypatch, tmp_path):
    calls: list[object] = []

    async def to_thread(function, *args, **kwargs):
        calls.append(function)
        return function(*args, **kwargs)

    async def chunks():
        yield b"1234"
        yield b"5678"

    monkeypatch.setattr(bot_module.asyncio, "to_thread", to_thread)
    path = tmp_path / "attachment.media"

    assert await _write_bounded_chunks(chunks(), path, max_bytes=8) == 8
    assert path.read_bytes() == b"12345678"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_attachment_download_rejects_non_discord_url_before_network(tmp_path):
    attachment = SimpleNamespace(url="https://example.com/private-media")

    with pytest.raises(UnsupportedInputError, match="URL was invalid"):
        await _download_attachment(attachment, tmp_path / "media", max_bytes=10)
