"""YouTube transcript-first ingestion with yt-dlp and AssemblyAI fallback."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
from youtube_transcript_api import (
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)
from youtube_transcript_api.proxies import GenericProxyConfig

from .config import Settings
from .errors import MediaLimitError, TranscriptUnavailableError, UnsupportedInputError
from .models import TranscriptArtifact
from .proxy import (
    YouTubeRequestIdentity,
    build_youtube_identity,
    is_proxy_transport_error,
)
from .transcription import AssemblyAITranscriber

logger = logging.getLogger(__name__)
YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)
YOUTUBE_TRANSCRIPT_TIMEOUT_SECONDS = 45
YT_DLP_SUBTITLE_TIMEOUT_SECONDS = 180
YT_DLP_METADATA_TIMEOUT_SECONDS = 90
YT_DLP_DOWNLOAD_TIMEOUT_SECONDS = 60 * 60
TIMESTAMP_PATTERN = re.compile(
    r"^\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3}\s*-->\s*"
    r"\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3}"
)
TAG_PATTERN = re.compile(r"<[^>]+>")


def validate_youtube_id(value: str) -> str:
    if not YOUTUBE_ID_PATTERN.fullmatch(value):
        raise UnsupportedInputError(
            "The YouTube URL does not contain a valid video ID."
        )
    return value


def extract_youtube_id(value: str) -> str:
    candidate = value.strip().strip("<>[](){}.,")
    if YOUTUBE_ID_PATTERN.fullmatch(candidate):
        return candidate
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        raise UnsupportedInputError("Only youtube.com and youtu.be URLs are supported.")
    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/", maxsplit=1)[0]
    else:
        segments = [segment for segment in parsed.path.split("/") if segment]
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif len(segments) >= 2 and segments[0] in {"shorts", "embed", "live"}:
            video_id = segments[1]
        else:
            video_id = ""
    return validate_youtube_id(video_id)


def parse_vtt_transcript(value: str) -> str:
    cues: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if not current:
            return
        cue = " ".join(current)
        cue = html.unescape(TAG_PATTERN.sub("", cue))
        cue = re.sub(r"\s+", " ", cue).strip()
        current.clear()
        if not cue:
            return
        if cues and cue == cues[-1]:
            return
        if cues and cue.startswith(cues[-1]):
            cues[-1] = cue
            return
        cues.append(cue)

    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if line == "WEBVTT" or line.startswith(("NOTE", "Kind:", "Language:")):
            continue
        if line.isdecimal() or TIMESTAMP_PATTERN.match(line):
            continue
        current.append(line)
    flush()
    transcript = " ".join(cues).strip()
    if not transcript:
        raise TranscriptUnavailableError("YouTube subtitles were empty.")
    return transcript


@dataclass(frozen=True)
class VideoMetadata:
    title: str
    channel: str | None
    duration: float | None


def _run_command(
    command: list[str],
    identity: YouTubeRequestIdentity,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - fixed executable and no shell
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=identity.subprocess_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise TranscriptUnavailableError("YouTube media retrieval timed out.") from exc
    except OSError as exc:
        raise TranscriptUnavailableError("yt-dlp could not be started.") from exc


class YouTubeService:
    def __init__(
        self,
        settings: Settings,
        assemblyai: AssemblyAITranscriber,
    ) -> None:
        self._settings = settings
        self._assemblyai = assemblyai

    def _youtube_api(self, identity: YouTubeRequestIdentity) -> YouTubeTranscriptApi:
        proxy = identity.proxy_value
        if not proxy:
            return YouTubeTranscriptApi()
        return YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy)
        )

    def _timed_transcript(
        self,
        video_id: str,
        identity: YouTubeRequestIdentity,
    ) -> str:
        api = self._youtube_api(identity)
        fetched = api.fetch(video_id, languages=["en"])
        text = " ".join(
            snippet.text.strip()
            for snippet in fetched
            if isinstance(snippet.text, str) and snippet.text.strip()
        )
        if not text:
            raise TranscriptUnavailableError("YouTube returned an empty transcript.")
        return text

    def _oembed_metadata(
        self,
        video_id: str,
        identity: YouTubeRequestIdentity,
    ) -> VideoMetadata:
        fallback = VideoMetadata(
            title=f"YouTube {video_id}", channel=None, duration=None
        )
        try:
            response = requests.get(
                "https://www.youtube.com/oembed",
                params={
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "format": "json",
                },
                headers={
                    "User-Agent": identity.user_agent,
                    "Accept-Language": identity.accept_language,
                },
                proxies=identity.requests_proxies,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            return fallback
        title = payload.get("title")
        author = payload.get("author_name")
        return VideoMetadata(
            title=title.strip()
            if isinstance(title, str) and title.strip()
            else fallback.title,
            channel=author.strip()
            if isinstance(author, str) and author.strip()
            else None,
            duration=None,
        )

    def _cookies_args(self) -> list[str]:
        path = self._settings.youtube_cookies_file
        return ["--cookies", str(path)] if path is not None else []

    def _subtitle_transcript(
        self,
        video_id: str,
        identity: YouTubeRequestIdentity,
        temp_dir: Path,
    ) -> str:
        output = temp_dir / "captions.%(ext)s"
        command = [
            "yt-dlp",
            "--no-playlist",
            "--no-cache-dir",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "en,en-*",
            "--sub-format",
            "vtt",
            "--quiet",
            *self._cookies_args(),
            *identity.ytdlp_args,
            "--output",
            str(output),
            f"https://youtu.be/{video_id}",
        ]
        result = _run_command(
            command,
            identity,
            timeout=YT_DLP_SUBTITLE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            error = RuntimeError(result.stderr[-2_000:])
            if is_proxy_transport_error(error):
                raise ProxyTransportFailure from error
            raise TranscriptUnavailableError("YouTube subtitles were unavailable.")
        candidates = sorted(temp_dir.glob("captions*.vtt"))
        if not candidates:
            raise TranscriptUnavailableError("YouTube subtitles were unavailable.")
        return parse_vtt_transcript(candidates[0].read_text("utf-8", errors="replace"))

    def _metadata_with_ytdlp(
        self,
        video_id: str,
        identity: YouTubeRequestIdentity,
    ) -> VideoMetadata:
        command = [
            "yt-dlp",
            "--no-playlist",
            "--no-cache-dir",
            "--skip-download",
            "--dump-single-json",
            "--quiet",
            *self._cookies_args(),
            *identity.ytdlp_args,
            f"https://youtu.be/{video_id}",
        ]
        result = _run_command(
            command, identity, timeout=YT_DLP_METADATA_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            error = RuntimeError(result.stderr[-2_000:])
            if is_proxy_transport_error(error):
                raise ProxyTransportFailure from error
            raise TranscriptUnavailableError("YouTube media metadata was unavailable.")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise TranscriptUnavailableError(
                "YouTube media metadata was invalid."
            ) from exc
        duration_value = payload.get("duration")
        duration = (
            float(duration_value)
            if isinstance(duration_value, (int, float)) and duration_value >= 0
            else None
        )
        if (
            duration is not None
            and duration > self._settings.max_media_duration_seconds
        ):
            raise MediaLimitError(
                "The YouTube video exceeds the configured duration limit."
            )
        live_status = payload.get("live_status")
        if live_status in {"is_live", "is_upcoming"}:
            raise TranscriptUnavailableError(
                "Live or upcoming YouTube videos cannot use completed-media fallback."
            )
        title = payload.get("title")
        channel = payload.get("channel") or payload.get("uploader")
        return VideoMetadata(
            title=title.strip()
            if isinstance(title, str) and title.strip()
            else f"YouTube {video_id}",
            channel=channel.strip()
            if isinstance(channel, str) and channel.strip()
            else None,
            duration=duration,
        )

    def _download_audio(
        self,
        video_id: str,
        identity: YouTubeRequestIdentity,
        temp_dir: Path,
        *,
        use_cookies: bool,
        android_vr: bool,
    ) -> Path:
        output = temp_dir / "audio.%(ext)s"
        extractor_args = (
            ["--extractor-args", "youtube:player_client=android_vr"]
            if android_vr
            else []
        )
        cookie_args = self._cookies_args() if use_cookies else []
        command = [
            "yt-dlp",
            "--no-playlist",
            "--no-cache-dir",
            "--no-progress",
            "--quiet",
            "--socket-timeout",
            "30",
            "--retries",
            "2",
            "--fragment-retries",
            "2",
            "--js-runtimes",
            "node",
            "--max-filesize",
            str(self._settings.max_attachment_bytes),
            "-f",
            "worstaudio/worst",
            *cookie_args,
            *extractor_args,
            *identity.ytdlp_args,
            "--output",
            str(output),
            f"https://youtu.be/{video_id}",
        ]
        result = _run_command(
            command, identity, timeout=YT_DLP_DOWNLOAD_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            error = RuntimeError(result.stderr[-2_000:])
            if is_proxy_transport_error(error):
                raise ProxyTransportFailure from error
            raise TranscriptUnavailableError("YouTube audio download failed.")
        candidates = [
            path
            for path in temp_dir.glob("audio.*")
            if path.is_file() and not path.name.endswith((".part", ".ytdl"))
        ]
        if not candidates:
            raise TranscriptUnavailableError("YouTube audio download produced no file.")
        audio = max(candidates, key=lambda path: path.stat().st_size)
        if audio.stat().st_size > self._settings.max_attachment_bytes:
            raise MediaLimitError(
                "The YouTube audio exceeds the configured size limit."
            )
        return audio

    async def transcribe(self, value: str) -> TranscriptArtifact:
        video_id = extract_youtube_id(value)
        identity = build_youtube_identity(self._settings)
        metadata = await asyncio.to_thread(self._oembed_metadata, video_id, identity)
        transcript: str | None = None
        source = "youtube"

        try:
            transcript = await asyncio.wait_for(
                asyncio.to_thread(self._timed_transcript, video_id, identity),
                timeout=YOUTUBE_TRANSCRIPT_TIMEOUT_SECONDS,
            )
        except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable):
            logger.info("Timed transcript unavailable for a YouTube video.")
        except (
            RequestBlocked,
            IpBlocked,
            requests.RequestException,
            TimeoutError,
        ) as exc:
            logger.info("Timed transcript request was blocked or unavailable.")
            if is_proxy_transport_error(exc) and self._settings.youtube_proxy_enabled:
                identity = build_youtube_identity(self._settings)
        except Exception:
            logger.info("Timed transcript path failed for a YouTube video.")

        with tempfile.TemporaryDirectory(prefix="youtube-transcription-") as raw_temp:
            temp_dir = Path(raw_temp)
            if transcript is None:
                try:
                    transcript = await asyncio.to_thread(
                        self._subtitle_transcript,
                        video_id,
                        identity,
                        temp_dir,
                    )
                    source = "youtube_subtitles"
                except (TranscriptUnavailableError, ProxyTransportFailure):
                    logger.info("Subtitle fallback unavailable for a YouTube video.")

            if transcript is None:
                profiles: list[tuple[YouTubeRequestIdentity, bool, bool]] = [
                    (identity, True, False)
                ]
                if self._settings.youtube_proxy_enabled:
                    profiles.append(
                        (build_youtube_identity(self._settings), False, True)
                    )
                    if self._settings.youtube_allow_direct_fallback:
                        profiles.append(
                            (
                                build_youtube_identity(self._settings, direct=True),
                                False,
                                True,
                            )
                        )
                elif self._settings.youtube_cookies_file is not None:
                    profiles.append((identity, False, True))

                audio: Path | None = None
                last_error: BaseException | None = None
                for profile_identity, use_cookies, android_vr in profiles:
                    try:
                        ytdlp_metadata = await asyncio.to_thread(
                            self._metadata_with_ytdlp,
                            video_id,
                            profile_identity,
                        )
                        metadata = ytdlp_metadata
                        audio = await asyncio.to_thread(
                            self._download_audio,
                            video_id,
                            profile_identity,
                            temp_dir,
                            use_cookies=use_cookies,
                            android_vr=android_vr,
                        )
                        break
                    except (TranscriptUnavailableError, ProxyTransportFailure) as exc:
                        last_error = exc
                        continue
                if audio is None:
                    raise TranscriptUnavailableError(
                        "YouTube captions and bounded audio fallback both failed."
                    ) from last_error
                transcript = await self._assemblyai.transcribe_file(audio)
                source = "assemblyai"

        if transcript is None:
            raise TranscriptUnavailableError("No YouTube transcript was produced.")
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        context_lines = [f"Title: {metadata.title}", f"URL: {watch_url}"]
        if metadata.channel:
            context_lines.append(f"Channel: {metadata.channel}")
        return TranscriptArtifact(
            title=metadata.title,
            source_url=watch_url,
            transcript=transcript,
            transcript_source=source,  # type: ignore[arg-type]
            summary_context="\n".join(context_lines),
            filename_stem=f"youtube-{video_id}",
            metadata={
                "Video ID": video_id,
                "Channel": metadata.channel or "Unknown",
            },
        )


class ProxyTransportFailure(RuntimeError):
    """Internal marker used only to rotate or stop a failed proxy identity."""
