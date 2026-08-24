from __future__ import annotations

import pytest
from pydantic import ValidationError

from youtube_transcription_bot.config import Settings


def test_settings_parse_explicit_channel_allowlist(settings_factory):
    settings = settings_factory(
        discord_allowed_channel_ids="123456789012345678,987654321098765432"
    )
    assert settings.discord_allowed_channel_ids == {
        123456789012345678,
        987654321098765432,
    }


def test_settings_reject_empty_channel_allowlist():
    with pytest.raises(ValidationError, match="explicitly allow"):
        Settings(
            discord_bot_token="not-real",  # noqa: S106 - test fixture
            discord_allowed_channel_ids="",
            assemblyai_api_key="not-real",
            openai_api_key="not-real",
        )


def test_settings_do_not_expose_secrets(settings_factory):
    settings = settings_factory()
    rendered = repr(settings)
    assert "discord-test-token-not-real" not in rendered
    assert "assembly-test-key-not-real" not in rendered
    assert "openai-test-key-not-real" not in rendered


def test_iproyal_requires_credentials(settings_factory):
    with pytest.raises(ValidationError, match="requires a username and password"):
        settings_factory(
            youtube_proxy_enabled=True,
            youtube_proxy_provider="iproyal",
            youtube_proxy_host="proxy.example",
            youtube_proxy_port=12321,
        )
