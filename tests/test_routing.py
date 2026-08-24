from __future__ import annotations

from types import SimpleNamespace

from youtube_transcription_bot.bot import JobKind, route_message


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
