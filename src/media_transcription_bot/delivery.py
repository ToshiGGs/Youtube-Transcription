"""Discord-limit-aware summary and transcript delivery."""

from __future__ import annotations

import io
import re

import discord

from .models import ProcessedArtifact, TranscriptArtifact

MAX_MESSAGE_CHARS = 1_999
MAX_EMBED_DESCRIPTION_CHARS = 4_096


def _safe_filename(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return normalized.strip("-._")[:120] or "media"


def transcript_document(artifact: TranscriptArtifact) -> str:
    lines = [
        f"Title: {artifact.title}",
        f"Transcript source: {artifact.transcript_source}",
    ]
    if artifact.source_url:
        lines.append(f"Source URL: {artifact.source_url}")
    lines.extend(f"{key}: {value}" for key, value in artifact.metadata.items())
    lines.extend(["", "Transcript:", artifact.transcript])
    return "\n".join(lines)


def text_file(content: str, filename: str) -> discord.File:
    return discord.File(
        io.BytesIO(content.encode("utf-8")),
        filename=_safe_filename(filename),
        description="Generated text output",
    )


async def send_processed_reply(
    message: discord.Message,
    result: ProcessedArtifact,
) -> None:
    transcript = result.transcript
    files = [
        text_file(
            transcript_document(transcript),
            f"transcript-{transcript.filename_stem}.txt",
        )
    ]
    allowed_mentions = discord.AllowedMentions.none()
    if len(result.summary) <= MAX_MESSAGE_CHARS:
        await message.reply(
            result.summary,
            mention_author=False,
            allowed_mentions=allowed_mentions,
            files=files,
        )
    elif len(result.summary) <= MAX_EMBED_DESCRIPTION_CHARS:
        await message.reply(
            embed=discord.Embed(description=result.summary),
            mention_author=False,
            allowed_mentions=allowed_mentions,
            files=files,
        )
    else:
        files.insert(0, text_file(result.summary, "summary.txt"))
        await message.reply(
            "The summary is attached because it exceeds Discord's embed limit.",
            mention_author=False,
            allowed_mentions=allowed_mentions,
            files=files,
        )


async def send_safe_error(message: discord.Message, detail: str) -> None:
    await message.reply(
        f"I couldn't process that media: {detail}",
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )
