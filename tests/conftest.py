from __future__ import annotations

from typing import Any

import pytest

from youtube_transcription_bot.config import Settings


@pytest.fixture
def settings_factory():
    def build(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "discord_bot_token": "discord-test-token-not-real",
            "discord_allowed_channel_ids": "123456789012345678",
            "assemblyai_api_key": "assembly-test-key-not-real",
            "openai_api_key": "openai-test-key-not-real",
        }
        values.update(overrides)
        return Settings(**values)

    return build
