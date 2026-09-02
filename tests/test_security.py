from __future__ import annotations

import pytest

from media_transcription_bot.errors import MediaLimitError, UnsafeRemoteUrlError
from media_transcription_bot.security import (
    _supported_media_type,
    _validate_media_response,
    is_public_ip,
    validate_public_host,
    validate_remote_url_shape,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/feed.xml",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/feed.xml",
        "file:///etc/passwd",
        "https://user:password@example.com/feed.xml",
        "https://example.com:8443/feed.xml",
    ],
)
def test_remote_url_shape_rejects_unsafe_targets(url):
    with pytest.raises(UnsafeRemoteUrlError):
        validate_remote_url_shape(url)


@pytest.mark.asyncio
async def test_public_host_rejects_literal_private_ip_without_network():
    with pytest.raises(UnsafeRemoteUrlError):
        await validate_public_host("http://10.0.0.1/feed.xml")


def test_public_ip_classifier_fails_closed():
    assert is_public_ip("8.8.8.8")
    assert not is_public_ip("100.64.0.1")
    assert not is_public_ip("not-an-ip")


def test_http_content_type_cannot_be_overridden_by_feed_declaration():
    assert not _supported_media_type("text/html", "audio/mpeg", ("audio/",))


def test_remote_media_requires_verifiable_size(settings_factory):
    with pytest.raises(MediaLimitError, match="verifiable"):
        _validate_media_response(
            final_url="https://cdn.example/episode.mp3",
            response_type="audio/mpeg",
            response_length=None,
            declared_type="audio/mpeg",
            declared_length=None,
            settings=settings_factory(),
            allowed_prefixes=("audio/",),
        )
