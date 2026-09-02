"""Allowlisted Discord message ingestion."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import tempfile
import time
from collections.abc import AsyncIterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import discord

from .config import Settings
from .delivery import send_processed_reply, send_safe_error
from .errors import MediaLimitError, UnsupportedInputError, UserVisibleError
from .heartbeat import write_heartbeat
from .models import ProcessedArtifact
from .processor import MediaProcessor
from .youtube import extract_youtube_id

logger = logging.getLogger(__name__)
URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
PODCAST_COMMAND_PATTERN = re.compile(r"^!podcast\s+(.+)$", re.IGNORECASE | re.DOTALL)
KNOWN_PODCAST_HOSTS = frozenset(
    {
        "open.spotify.com",
        "podcasts.apple.com",
        "itunes.apple.com",
    }
)
SUPPORTED_MEDIA_EXTENSIONS = frozenset(
    {
        ".aac",
        ".flac",
        ".m4a",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogg",
        ".opus",
        ".wav",
        ".webm",
    }
)
HEARTBEAT_INTERVAL_SECONDS = 30
ATTACHMENT_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS = 30 * 60
DISCORD_CDN_HOSTS = frozenset(
    {
        "cdn.discord.com",
        "cdn.discordapp.com",
        "media.discordapp.net",
    }
)


class JobKind(StrEnum):
    YOUTUBE = "youtube"
    PODCAST = "podcast"
    ATTACHMENT = "attachment"


@dataclass(frozen=True)
class MessageJob:
    kind: JobKind
    value: str | discord.Attachment


def _trim_url(value: str) -> str:
    return value.rstrip(".,;:!?)]}")


def _known_podcast_url(value: str) -> bool:
    host = (urlsplit(value).hostname or "").lower()
    return host in KNOWN_PODCAST_HOSTS or host.endswith(".podcasts.apple.com")


def _supported_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith(("audio/", "video/")):
        return True
    return Path(attachment.filename).suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS


def route_message(message: discord.Message) -> MessageJob | None:
    command = PODCAST_COMMAND_PATTERN.match(message.content.strip())
    if command:
        value = command.group(1).strip()
        if value:
            return MessageJob(JobKind.PODCAST, value)

    for raw_url in URL_PATTERN.findall(message.content):
        url = _trim_url(raw_url)
        try:
            extract_youtube_id(url)
        except UnsupportedInputError:
            pass
        else:
            return MessageJob(JobKind.YOUTUBE, url)
        if _known_podcast_url(url):
            return MessageJob(JobKind.PODCAST, url)

    supported = [item for item in message.attachments if _supported_attachment(item)]
    if supported:
        return MessageJob(JobKind.ATTACHMENT, supported[0])
    return None


async def _write_bounded_chunks(
    chunks: AsyncIterable[bytes],
    path: Path,
    max_bytes: int,
) -> int:
    bytes_written = 0
    with path.open("wb") as output:
        async for chunk in chunks:
            if not chunk:
                continue
            bytes_written += len(chunk)
            if bytes_written > max_bytes:
                raise MediaLimitError(
                    "The attachment exceeds the configured size limit."
                )
            await asyncio.to_thread(output.write, chunk)
    if bytes_written == 0:
        raise MediaLimitError("The attachment download was empty.")
    return bytes_written


async def _download_attachment(
    attachment: discord.Attachment,
    path: Path,
    max_bytes: int,
) -> None:
    parsed = urlsplit(attachment.url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in DISCORD_CDN_HOSTS
    ):
        raise UnsupportedInputError("The Discord attachment URL was invalid.")

    timeout = aiohttp.ClientTimeout(
        total=ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS,
        connect=30,
        sock_read=120,
    )
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        async with session.get(attachment.url, allow_redirects=False) as response:
            if response.status != 200:
                raise UserVisibleError(
                    "The Discord attachment could not be downloaded."
                )
            declared_length = response.headers.get("Content-Length")
            if declared_length:
                try:
                    declared_bytes = int(declared_length)
                except ValueError:
                    declared_bytes = None
                if declared_bytes is not None and declared_bytes > max_bytes:
                    raise MediaLimitError(
                        "The attachment exceeds the configured size limit."
                    )
            await _write_bounded_chunks(
                response.content.iter_chunked(ATTACHMENT_DOWNLOAD_CHUNK_BYTES),
                path,
                max_bytes,
            )


class TranscriptionBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            intents=intents, allowed_mentions=discord.AllowedMentions.none()
        )
        self._settings = settings
        self._processor = MediaProcessor(settings)
        self._job_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._recent_messages: dict[int, float] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="bot-heartbeat",
        )

    async def close(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        await self._processor.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Discord bot is connected and ready.")

    async def _heartbeat_loop(self) -> None:
        while True:
            if self.is_ready() and not self.is_closed():
                try:
                    await asyncio.to_thread(write_heartbeat)
                except OSError:
                    logger.warning("Bot heartbeat could not be written.")
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

    def _first_delivery(self, message_id: int) -> bool:
        now = time.monotonic()
        expired = [
            key for key, seen in self._recent_messages.items() if now - seen > 900
        ]
        for key in expired:
            self._recent_messages.pop(key, None)
        if message_id in self._recent_messages:
            return False
        self._recent_messages[message_id] = now
        return True

    async def on_message(self, message: discord.Message) -> None:
        if (
            message.author.bot
            or message.webhook_id is not None
            or message.channel.id not in self._settings.discord_allowed_channel_ids
            or not self._first_delivery(message.id)
        ):
            return
        job = route_message(message)
        if job is None:
            return

        async with self._job_semaphore, message.channel.typing():
            try:
                if job.kind == JobKind.YOUTUBE:
                    result = await self._processor.process_youtube(str(job.value))
                elif job.kind == JobKind.PODCAST:
                    result = await self._processor.process_podcast(str(job.value))
                else:
                    if not isinstance(job.value, discord.Attachment):
                        raise UnsupportedInputError(
                            "The Discord attachment was invalid."
                        )
                    result = await self._process_attachment(job.value)
                await send_processed_reply(message, result)
            except UserVisibleError as exc:
                logger.info("Media job ended with a safe user-visible failure.")
                await send_safe_error(message, str(exc))
            except Exception as exc:
                # Exception text and traceback can contain signed URLs or provider
                # diagnostics. Only the class name is safe for default logs.
                logger.error(
                    "Media job failed with internal error type=%s.",
                    type(exc).__name__,
                )
                await send_safe_error(
                    message,
                    "an internal error occurred; check the sanitized service logs.",
                )

    async def _process_attachment(
        self, attachment: discord.Attachment
    ) -> ProcessedArtifact:
        if attachment.size <= 0:
            raise MediaLimitError("The attachment is empty.")
        if attachment.size > self._settings.max_attachment_bytes:
            raise MediaLimitError("The attachment exceeds the configured size limit.")
        content_type = attachment.content_type or ""
        if not content_type or content_type == "application/octet-stream":
            guessed, _ = mimetypes.guess_type(attachment.filename)
            content_type = guessed or content_type
        if not content_type.startswith(("audio/", "video/")):
            raise UnsupportedInputError(
                "Only audio and video attachments are supported."
            )

        suffix = Path(attachment.filename).suffix.lower()
        if suffix not in SUPPORTED_MEDIA_EXTENSIONS:
            suffix = ".media"
        with tempfile.TemporaryDirectory(prefix="discord-media-") as raw_temp:
            path = Path(raw_temp) / f"attachment{suffix}"
            await _download_attachment(
                attachment,
                path,
                self._settings.max_attachment_bytes,
            )
            if not path.is_file() or path.stat().st_size <= 0:
                raise MediaLimitError("The attachment download was empty.")
            if path.stat().st_size > self._settings.max_attachment_bytes:
                raise MediaLimitError(
                    "The attachment exceeds the configured size limit."
                )
            return await self._processor.process_attachment(
                path,
                filename=attachment.filename,
                content_type=content_type,
            )
