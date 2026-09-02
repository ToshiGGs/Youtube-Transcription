from __future__ import annotations

import subprocess

import pytest

from media_transcription_bot.errors import MediaLimitError
from media_transcription_bot.transcription import validate_local_media_duration


@pytest.mark.asyncio
async def test_local_media_duration_limit_is_enforced(
    monkeypatch,
    settings_factory,
    tmp_path,
):
    media = tmp_path / "audio.mp3"
    media.write_bytes(b"test")

    def completed(*args, **kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="121\n")

    monkeypatch.setattr(subprocess, "run", completed)
    settings = settings_factory(max_media_duration_seconds=120)

    with pytest.raises(MediaLimitError, match="duration"):
        await validate_local_media_duration(media, settings)
