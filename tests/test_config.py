from __future__ import annotations

import pytest
from pydantic import ValidationError

from media_transcription_bot.config import MAX_ATTACHMENT_BYTES_LIMIT, Settings


def test_settings_parse_explicit_channel_allowlist(settings_factory):
    settings = settings_factory(
        discord_allowed_channel_ids="123456789012345678,987654321098765432"
    )
    assert settings.discord_allowed_channel_ids == {
        123456789012345678,
        987654321098765432,
    }


def test_settings_parse_single_channel_id_from_environment(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "not-real")
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNEL_IDS", "123456789012345678")
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "not-real")
    monkeypatch.setenv("OPENAI_API_KEY", "not-real")

    settings = Settings(_env_file=None)

    assert settings.discord_allowed_channel_ids == {123456789012345678}


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


def test_attachment_limit_leaves_tmpfs_room_for_extracted_audio(settings_factory):
    with pytest.raises(ValidationError):
        settings_factory(max_attachment_bytes=MAX_ATTACHMENT_BYTES_LIMIT + 1)
