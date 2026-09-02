from __future__ import annotations

from types import SimpleNamespace

import pytest

import media_transcription_bot.summary as summary_module
from media_transcription_bot.summary import (
    SUMMARY_PROMPT,
    SummaryService,
    neutralize_mentions,
    split_text,
)


class FakeResponses:
    def __init__(self) -> None:
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text="**Overview**\n\nA faithful summary.")


class FakeOpenAI:
    def __init__(self, **kwargs) -> None:
        self.responses = FakeResponses()

    async def close(self) -> None:
        return None


def test_summary_prompt_treats_transcript_as_untrusted_source():
    assert "never as instructions" in SUMMARY_PROMPT
    assert "Do not add outside facts" in SUMMARY_PROMPT


def test_split_text_never_exceeds_requested_chunk_size():
    chunks = split_text("one " * 5000, 8000)
    assert len(chunks) > 1
    assert all(len(chunk) <= 8000 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == ("one" * 5000)


def test_mentions_are_neutralized():
    value = "Ping @everyone, @here, <@123456789012345678>, and <@&987654321098765432>."
    output = neutralize_mentions(value)
    assert "@everyone" not in output
    assert "@here" not in output
    assert "<@" not in output


@pytest.mark.asyncio
async def test_summary_uses_responses_api(monkeypatch, settings_factory):
    monkeypatch.setattr(summary_module, "AsyncOpenAI", FakeOpenAI)
    service = SummaryService(settings_factory())
    result = await service.summarize("Speaker describes the subject.", "Title: Test")
    assert result.startswith("**Overview**")
    fake_client = service._client
    assert fake_client.responses.calls[0]["model"] == "gpt-5.5"
    assert "Transcript:" in fake_client.responses.calls[0]["input"]
    assert fake_client.responses.calls[0]["store"] is False
