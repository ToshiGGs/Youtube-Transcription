"""YouTube transcript-first ingestion with yt-dlp and AssemblyAI fallback."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import re
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
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
YOUTUBE_TRANSCRIPT_REQUEST_TIMEOUT_SECONDS = 30
YT_DLP_SUBTITLE_TIMEOUT_SECONDS = 180
YT_DLP_METADATA_TIMEOUT_SECONDS = 90
YT_DLP_DOWNLOAD_TIMEOUT_SECONDS = 60 * 60
TIMESTAMP_PATTERN = re.compile(
    r"^\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3}\s*-->\s*"
    r"\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3}"
)
TAG_PATTERN = re.compile(r"<[^>]+>")
TRANSCRIPT_WORD_PATTERN = re.compile(r"[a-z0-9]+", flags=re.IGNORECASE)
CAPTION_WINDOW_MIN_WORDS = 6
CAPTION_WINDOW_MAX_WORDS = 16
CAPTION_WINDOW_MIN_REPEATS = 3
CAPTION_LINE_OVERLAP_MIN_WORDS = 4


class _TimeoutSession(requests.Session):
    def __init__(self, timeout_seconds: float) -> None:
        super().__init__()
        self._timeout_seconds = timeout_seconds

    def request(
        self,
        method: str | bytes,
        url: str | bytes,
        *args: Any,
        **kwargs: Any,
    ) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout_seconds)
        return super().request(method, url, *args, **kwargs)


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


def _transcript_words(text: str) -> list[str]:
    return TRANSCRIPT_WORD_PATTERN.findall(text.lower())


def _find_repeated_caption_window(text: str) -> tuple[int, int] | None:
    word_matches = list(TRANSCRIPT_WORD_PATTERN.finditer(text))
    words = [match.group().lower() for match in word_matches]
    if len(words) < CAPTION_WINDOW_MIN_WORDS * CAPTION_WINDOW_MIN_REPEATS:
        return None

    for phrase_len in range(
        CAPTION_WINDOW_MAX_WORDS,
        CAPTION_WINDOW_MIN_WORDS - 1,
        -1,
    ):
        max_start = len(words) - (phrase_len * CAPTION_WINDOW_MIN_REPEATS)
        for start in range(max_start + 1):
            phrase = words[start : start + phrase_len]
            repeat_count = 1
            next_start = start + phrase_len
            while words[next_start : next_start + phrase_len] == phrase:
                repeat_count += 1
                next_start += phrase_len
            if repeat_count >= CAPTION_WINDOW_MIN_REPEATS and next_start < len(words):
                keep_word_index = start + ((repeat_count - 1) * phrase_len)
                return (
                    word_matches[start].start(),
                    word_matches[keep_word_index].start(),
                )
    return None


def _collapse_repeated_caption_windows(text: str) -> str:
    collapsed = text
    while True:
        repeat = _find_repeated_caption_window(collapsed)
        if repeat is None:
            return collapsed
        duplicate_start, keep_start = repeat
        prefix = collapsed[:duplicate_start].rstrip()
        suffix = collapsed[keep_start:].lstrip()
        collapsed = f"{prefix} {suffix}".strip() if prefix else suffix


def _words_start_with(words: list[str], prefix: list[str]) -> bool:
    return (
        len(prefix) >= CAPTION_LINE_OVERLAP_MIN_WORDS
        and len(words) >= len(prefix)
        and words[: len(prefix)] == prefix
    )


def _shared_word_overlap_length(
    left_words: list[str],
    right_words: list[str],
) -> int:
    max_overlap = min(len(left_words), len(right_words))
    for overlap in range(max_overlap, CAPTION_LINE_OVERLAP_MIN_WORDS - 1, -1):
        if left_words[-overlap:] == right_words[:overlap]:
            return overlap
    return 0


def _remove_word_prefix(text: str, word_count: int) -> str:
    word_matches = list(TRANSCRIPT_WORD_PATTERN.finditer(text))
    if word_count <= 0:
        return text
    if word_count >= len(word_matches):
        return ""
    return text[word_matches[word_count - 1].end() :].lstrip()


def _append_caption_suffix(text: str, suffix: str) -> str:
    separator = "" if suffix[:1] in ",.;:!?" else " "
    return f"{text}{separator}{suffix}"


def normalize_caption_lines(lines: Iterable[str]) -> str:
    normalized_lines: list[str] = []
    normalized_words: list[list[str]] = []
    for line in lines:
        normalized_line = " ".join(line.split())
        normalized_line = _collapse_repeated_caption_windows(normalized_line)
        if not normalized_line:
            continue

        current_words = _transcript_words(normalized_line)
        if normalized_lines:
            previous_words = normalized_words[-1]
            if previous_words == current_words:
                continue
            if _words_start_with(current_words, previous_words):
                normalized_lines[-1] = normalized_line
                normalized_words[-1] = current_words
                continue
            if _words_start_with(previous_words, current_words):
                continue

            overlap = _shared_word_overlap_length(previous_words, current_words)
            if overlap:
                suffix = _remove_word_prefix(normalized_line, overlap)
                if suffix:
                    normalized_lines[-1] = _append_caption_suffix(
                        normalized_lines[-1], suffix
                    )
                    normalized_words[-1] = _transcript_words(normalized_lines[-1])
                continue

        normalized_lines.append(normalized_line)
        normalized_words.append(current_words)
    return " ".join(normalized_lines)


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
    transcript = normalize_caption_lines(cues).strip()
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

    def _youtube_api(
        self,
        identity: YouTubeRequestIdentity,
        http_client: requests.Session,
    ) -> YouTubeTranscriptApi:
        proxy = identity.proxy_value
        if not proxy:
            return YouTubeTranscriptApi(http_client=http_client)
        return YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy),
            http_client=http_client,
        )

    def _timed_transcript(
        self,
        video_id: str,
        identity: YouTubeRequestIdentity,
    ) -> str:
        with _TimeoutSession(YOUTUBE_TRANSCRIPT_REQUEST_TIMEOUT_SECONDS) as client:
            client.headers.update(
                {
                    "User-Agent": identity.user_agent,
                    "Accept-Language": identity.accept_language,
                }
            )
            api = self._youtube_api(identity, client)
            fetched = api.fetch(video_id, languages=["en"])
            snippets: list[tuple[float, str]] = []
            for snippet in fetched:
                snippet_text = getattr(snippet, "text", None)
                if not isinstance(snippet_text, str) or not snippet_text.strip():
                    continue
                try:
                    start = float(snippet.start)
                except (AttributeError, TypeError, ValueError) as exc:
                    raise TranscriptUnavailableError(
                        "YouTube returned invalid transcript timing."
                    ) from exc
                if not math.isfinite(start) or start < 0:
                    raise TranscriptUnavailableError(
                        "YouTube returned invalid transcript timing."
                    )
                snippets.append(
                    (start, html.unescape(TAG_PATTERN.sub("", snippet_text)))
                )
            snippets.sort(key=lambda item: item[0])
            text = normalize_caption_lines(
                snippet_text for _, snippet_text in snippets
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
        *,
        use_cookies: bool,
    ) -> VideoMetadata:
        command = [
            "yt-dlp",
            "--no-playlist",
            "--no-cache-dir",
            "--skip-download",
            "--dump-single-json",
            "--quiet",
            *(self._cookies_args() if use_cookies else []),
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
                except ProxyTransportFailure:
                    logger.info("Subtitle fallback unavailable for a YouTube video.")
                    if self._settings.youtube_proxy_enabled:
                        identity = build_youtube_identity(self._settings)
                except TranscriptUnavailableError:
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
                            use_cookies=use_cookies,
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
