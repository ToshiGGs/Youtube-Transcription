"""Public RSS resolution and AssemblyAI podcast transcription."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import cast
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from xml.etree.ElementTree import Element

import aiohttp
from defusedxml import ElementTree as SafeElementTree

from .config import Settings
from .errors import MediaLimitError, PodcastResolutionError, UnsafeRemoteUrlError
from .models import PodcastEpisode, TranscriptArtifact
from .security import fetch_public_text, validate_remote_media
from .transcription import AssemblyAITranscriber

SPOTIFY_PATTERN = re.compile(
    r"^(?:https?://open\.spotify\.com/(episode|show)/|spotify:(episode|show):)"
    r"([A-Za-z0-9]+)",
    re.IGNORECASE,
)
APPLE_ID_PATTERN = re.compile(r"/id(\d+)", re.IGNORECASE)
RSS_TYPES = frozenset({"application/rss+xml", "application/xml", "text/xml"})
SEARCH_RESULT_LIMIT = 10
EPISODE_MIN_SCORE = 0.70
AMBIGUITY_DELTA = 0.04


@dataclass(frozen=True)
class EpisodeTarget:
    title: str | None = None
    show_title: str | None = None
    publisher: str | None = None
    release_date: date | None = None
    duration_seconds: float | None = None
    item_url: str | None = None
    spotify_show_id: str | None = None


@dataclass(frozen=True)
class FeedCandidate:
    url: str
    title: str | None = None
    author: str | None = None


@dataclass(frozen=True)
class ParsedFeed:
    url: str
    title: str | None
    author: str | None
    episodes: tuple[PodcastEpisode, ...]


class RSSLinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.feed_urls: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "link":
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        if (
            "alternate" in values.get("rel", "").lower().split()
            and values.get("type", "").lower() in RSS_TYPES
            and values.get("href")
        ):
            self.feed_urls.append(urljoin(self.base_url, values["href"]))


def _child(element: Element, name: str) -> Element | None:
    return next(
        (
            child
            for child in element
            if isinstance(child.tag, str)
            and (child.tag == name or child.tag.rsplit("}", maxsplit=1)[-1] == name)
        ),
        None,
    )


def _children(element: Element, name: str) -> list[Element]:
    return [
        child
        for child in element
        if isinstance(child.tag, str)
        and (child.tag == name or child.tag.rsplit("}", maxsplit=1)[-1] == name)
    ]


def _text(element: Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _duration(value: str | None) -> float | None:
    if not value:
        return None
    parts = value.strip().split(":")
    try:
        total = 0.0
        for part in parts:
            total = total * 60 + float(part)
        return total if total >= 0 else None
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def parse_rss(xml_text: str, feed_url: str) -> ParsedFeed:
    root = SafeElementTree.fromstring(xml_text)
    channel = _child(root, "channel")
    if channel is None:
        raise PodcastResolutionError("The URL did not return a supported RSS feed.")
    title = _text(_child(channel, "title"))
    author = _text(_child(channel, "author"))
    episodes: list[PodcastEpisode] = []
    for item in _children(channel, "item"):
        enclosure = _child(item, "enclosure")
        if enclosure is None or not enclosure.attrib.get("url"):
            continue
        episode_title = _text(_child(item, "title")) or "Untitled episode"
        link = _text(_child(item, "link"))
        episodes.append(
            PodcastEpisode(
                title=episode_title,
                feed_url=feed_url,
                feed_title=title,
                link=urljoin(feed_url, link) if link else None,
                guid=_text(_child(item, "guid")),
                published_at=_datetime(
                    _text(_child(item, "pubDate")) or _text(_child(item, "published"))
                ),
                duration_seconds=_duration(_text(_child(item, "duration"))),
                enclosure_url=urljoin(feed_url, enclosure.attrib["url"]),
                enclosure_type=enclosure.attrib.get("type"),
                enclosure_length=_int(enclosure.attrib.get("length")),
            )
        )
    if not episodes:
        raise PodcastResolutionError("The RSS feed contained no media episodes.")
    return ParsedFeed(feed_url, title, author, tuple(episodes))


def _normalize(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _similarity(left: str | None, right: str | None) -> float:
    left_norm = _normalize(left)
    right_norm = _normalize(right)
    if not left_norm or not right_norm:
        return 0.0
    return (
        1.0
        if left_norm == right_norm
        else SequenceMatcher(None, left_norm, right_norm).ratio()
    )


def _canonical_url(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    return (
        parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            fragment="",
        )
        .geturl()
        .rstrip("/")
    )


def _episode_score(target: EpisodeTarget, episode: PodcastEpisode) -> float:
    if target.item_url and _canonical_url(target.item_url) in {
        _canonical_url(episode.link),
        _canonical_url(episode.guid),
        _canonical_url(episode.enclosure_url),
    }:
        return 1.0
    scores: list[tuple[float, float]] = []
    if target.title:
        scores.append((_similarity(target.title, episode.title), 0.65))
    if target.release_date and episode.published_at:
        delta = abs((target.release_date - episode.published_at.date()).days)
        scores.append(
            (
                1.0
                if delta == 0
                else 0.6
                if delta <= 2
                else 0.3
                if delta <= 7
                else 0.0,
                0.2,
            )
        )
    if target.duration_seconds and episode.duration_seconds:
        delta_seconds = abs(target.duration_seconds - episode.duration_seconds)
        scores.append(
            (
                1.0
                if delta_seconds <= 60
                else 0.7
                if delta_seconds <= 180
                else 0.4
                if delta_seconds <= 300
                else 0.0,
                0.15,
            )
        )
    weight = sum(item[1] for item in scores)
    return (
        sum(value * item_weight for value, item_weight in scores) / weight
        if weight
        else 0.0
    )


async def _read_json_response(
    response: aiohttp.ClientResponse, max_bytes: int = 2_000_000
) -> dict[str, object]:
    raw = await response.content.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise PodcastResolutionError("A podcast metadata response was too large.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PodcastResolutionError(
            "A podcast metadata response was invalid."
        ) from exc
    if not isinstance(payload, dict):
        raise PodcastResolutionError("A podcast metadata response was invalid.")
    return payload


class PodcastResolver:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _fetch_feed(self, url: str) -> ParsedFeed:
        text, final_url, _ = await fetch_public_text(
            url,
            self._settings,
            label="Podcast RSS feed",
            accept="application/rss+xml, application/xml, text/xml, */*",
        )
        try:
            return await asyncio.to_thread(parse_rss, text, final_url)
        except PodcastResolutionError:
            raise
        except Exception as exc:
            raise PodcastResolutionError(
                "The URL did not return a supported RSS feed."
            ) from exc

    async def _spotify_target(self, episode_id: str) -> EpisodeTarget:
        if not self._settings.spotify_enabled:
            raise PodcastResolutionError(
                "Spotify episode URLs require SPOTIFY_CLIENT_ID and "
                "SPOTIFY_CLIENT_SECRET."
            )
        credential = (
            f"{self._settings.spotify_client_id}:"
            f"{self._settings.spotify_client_secret.get_secret_value()}"
        ).encode()
        timeout = aiohttp.ClientTimeout(
            total=self._settings.remote_request_timeout_seconds
        )
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                headers={
                    "Authorization": f"Basic {base64.b64encode(credential).decode()}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
            ) as response:
                if not 200 <= response.status < 300:
                    raise PodcastResolutionError("Spotify authentication failed.")
                token_payload = await _read_json_response(response)
            token = token_payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise PodcastResolutionError(
                    "Spotify authentication returned no token."
                )
            async with session.get(
                f"https://api.spotify.com/v1/episodes/{episode_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"market": self._settings.spotify_market},
            ) as response:
                if not 200 <= response.status < 300:
                    raise PodcastResolutionError("The Spotify episode was unavailable.")
                payload = await _read_json_response(response)
        show_value = payload.get("show")
        show = (
            cast(dict[str, object], show_value) if isinstance(show_value, dict) else {}
        )
        duration_ms = payload.get("duration_ms")
        release = payload.get("release_date")
        release_date = None
        if isinstance(release, str):
            try:
                release_date = date.fromisoformat(release[:10])
            except ValueError:
                release_date = None
        return EpisodeTarget(
            title=_string(payload.get("name")),
            show_title=_string(show.get("name")),
            publisher=_string(show.get("publisher")),
            release_date=release_date,
            duration_seconds=float(duration_ms) / 1000
            if isinstance(duration_ms, (int, float))
            else None,
            spotify_show_id=_string(show.get("id")),
        )

    async def _apple_json(self, path: str, params: dict[str, str]) -> dict[str, object]:
        url = f"https://itunes.apple.com/{path}?{urlencode(params)}"
        text, _, _ = await fetch_public_text(
            url,
            self._settings,
            label="Apple Podcasts metadata",
            accept="application/json",
        )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PodcastResolutionError(
                "Apple Podcasts returned invalid metadata."
            ) from exc
        if not isinstance(payload, dict):
            raise PodcastResolutionError("Apple Podcasts returned invalid metadata.")
        return payload

    async def _apple_target(
        self,
        collection_id: str,
        episode_id: str | None,
    ) -> tuple[EpisodeTarget, list[FeedCandidate]]:
        payload = await self._apple_json(
            "lookup",
            {"id": collection_id, "entity": "podcastEpisode", "limit": "200"},
        )
        results = payload.get("results")
        if not isinstance(results, list):
            raise PodcastResolutionError("Apple Podcasts returned no matching show.")
        feeds: list[FeedCandidate] = []
        target = EpisodeTarget()
        for raw in results:
            if not isinstance(raw, dict):
                continue
            feed_url = raw.get("feedUrl")
            if isinstance(feed_url, str) and feed_url:
                feeds.append(
                    FeedCandidate(
                        feed_url,
                        raw.get("collectionName")
                        if isinstance(raw.get("collectionName"), str)
                        else None,
                        raw.get("artistName")
                        if isinstance(raw.get("artistName"), str)
                        else None,
                    )
                )
            track_id = raw.get("trackId")
            if episode_id and str(track_id) == episode_id:
                duration_ms = raw.get("trackTimeMillis")
                release = raw.get("releaseDate")
                release_date = None
                if isinstance(release, str):
                    try:
                        release_date = datetime.fromisoformat(
                            release.replace("Z", "+00:00")
                        ).date()
                    except ValueError:
                        release_date = None
                target = EpisodeTarget(
                    title=raw.get("trackName")
                    if isinstance(raw.get("trackName"), str)
                    else None,
                    show_title=raw.get("collectionName")
                    if isinstance(raw.get("collectionName"), str)
                    else None,
                    publisher=raw.get("artistName")
                    if isinstance(raw.get("artistName"), str)
                    else None,
                    release_date=release_date,
                    duration_seconds=float(duration_ms) / 1000
                    if isinstance(duration_ms, (int, float))
                    else None,
                    item_url=raw.get("trackViewUrl")
                    if isinstance(raw.get("trackViewUrl"), str)
                    else None,
                )
        return target, feeds

    async def _search_feeds(self, query: str) -> list[FeedCandidate]:
        candidates: list[FeedCandidate] = []
        try:
            payload = await self._apple_json(
                "search",
                {
                    "term": query,
                    "media": "podcast",
                    "entity": "podcast",
                    "limit": str(SEARCH_RESULT_LIMIT),
                },
            )
            results = payload.get("results")
            if isinstance(results, list):
                for raw in results:
                    if not isinstance(raw, dict) or not isinstance(
                        raw.get("feedUrl"), str
                    ):
                        continue
                    candidates.append(
                        FeedCandidate(
                            raw["feedUrl"],
                            raw.get("collectionName")
                            if isinstance(raw.get("collectionName"), str)
                            else None,
                            raw.get("artistName")
                            if isinstance(raw.get("artistName"), str)
                            else None,
                        )
                    )
        except (PodcastResolutionError, UnsafeRemoteUrlError):
            pass

        if self._settings.podcast_index_enabled:
            timestamp = str(int(time.time()))
            secret = self._settings.podcast_index_api_secret.get_secret_value()
            digest = hashlib.sha1(  # noqa: S324 - required by PodcastIndex auth
                (self._settings.podcast_index_api_key + secret + timestamp).encode(),
                usedforsecurity=False,
            ).hexdigest()
            headers = {
                "X-Auth-Date": timestamp,
                "X-Auth-Key": self._settings.podcast_index_api_key,
                "Authorization": digest,
                "User-Agent": "YoutubeTranscriptionBot/1.0",
            }
            timeout = aiohttp.ClientTimeout(
                total=self._settings.remote_request_timeout_seconds
            )
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=False
            ) as session:
                async with session.get(
                    "https://api.podcastindex.org/api/1.0/search/byterm",
                    headers=headers,
                    params={"q": query, "max": str(SEARCH_RESULT_LIMIT)},
                ) as response:
                    if 200 <= response.status < 300:
                        payload = await _read_json_response(response)
                        feeds = payload.get("feeds")
                        if isinstance(feeds, list):
                            for raw in feeds:
                                if not isinstance(raw, dict) or not isinstance(
                                    raw.get("url"), str
                                ):
                                    continue
                                candidates.append(
                                    FeedCandidate(
                                        raw["url"],
                                        raw.get("title")
                                        if isinstance(raw.get("title"), str)
                                        else None,
                                        raw.get("author")
                                        if isinstance(raw.get("author"), str)
                                        else None,
                                    )
                                )
        unique: dict[str, FeedCandidate] = {}
        for candidate in candidates:
            unique.setdefault(_canonical_url(candidate.url), candidate)
        return list(unique.values())

    async def _search_target_and_feeds(
        self, query: str
    ) -> tuple[EpisodeTarget, list[FeedCandidate]]:
        show_hint, separator, episode_hint = query.partition("|")
        target_title = episode_hint.strip() if separator else query.strip()
        feed_query = show_hint.strip() if separator else query.strip()
        target = EpisodeTarget(
            title=target_title, show_title=show_hint.strip() if separator else None
        )
        feeds = await self._search_feeds(feed_query)
        if not feeds:
            payload = await self._apple_json(
                "search",
                {
                    "term": query,
                    "media": "podcast",
                    "entity": "podcastEpisode",
                    "limit": str(SEARCH_RESULT_LIMIT),
                },
            )
            results = payload.get("results")
            if isinstance(results, list):
                for raw in results:
                    if not isinstance(raw, dict):
                        continue
                    feed_url = raw.get("feedUrl")
                    if isinstance(feed_url, str):
                        feeds.append(FeedCandidate(feed_url))
                    if target.title == query and isinstance(raw.get("trackName"), str):
                        target = EpisodeTarget(
                            title=raw["trackName"],
                            show_title=raw.get("collectionName")
                            if isinstance(raw.get("collectionName"), str)
                            else None,
                        )
                        break
        return target, feeds

    async def _autodiscover(self, page_url: str) -> list[FeedCandidate]:
        text, final_url, content_type = await fetch_public_text(
            page_url,
            self._settings,
            label="Podcast publisher page",
            accept="text/html, application/xhtml+xml",
        )
        if "html" not in (content_type or "").lower():
            return []
        parser = RSSLinkParser(final_url)
        parser.feed(text)
        return [FeedCandidate(url) for url in parser.feed_urls]

    async def _select(
        self,
        target: EpisodeTarget | None,
        feeds: list[FeedCandidate],
    ) -> PodcastEpisode:
        unique: dict[str, FeedCandidate] = {}
        for candidate in feeds[:25]:
            unique.setdefault(_canonical_url(candidate.url), candidate)

        parsed_feeds: list[ParsedFeed] = []
        for candidate in unique.values():
            try:
                parsed_feeds.append(await self._fetch_feed(candidate.url))
            except (PodcastResolutionError, UnsafeRemoteUrlError):
                continue
        if not parsed_feeds:
            raise PodcastResolutionError(
                "No public podcast RSS feed could be resolved."
            )
        if target is None:
            episodes = [episode for feed in parsed_feeds for episode in feed.episodes]
            return max(
                episodes,
                key=lambda episode: (
                    episode.published_at or datetime.min.replace(tzinfo=UTC)
                ),
            )

        scored = sorted(
            (
                (_episode_score(target, episode), episode)
                for feed in parsed_feeds
                for episode in feed.episodes
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if not scored or scored[0][0] < EPISODE_MIN_SCORE:
            raise PodcastResolutionError(
                "No sufficiently confident podcast episode match was found."
            )
        if len(scored) > 1 and scored[0][0] - scored[1][0] < AMBIGUITY_DELTA:
            raise PodcastResolutionError(
                "The podcast search was ambiguous; use `!podcast Show | Episode`."
            )
        return scored[0][1]

    async def resolve(self, value: str) -> PodcastEpisode:
        raw = value.strip().strip("<>")
        spotify = SPOTIFY_PATTERN.match(raw)
        if spotify:
            object_type = spotify.group(1) or spotify.group(2)
            if object_type.lower() != "episode":
                raise PodcastResolutionError(
                    "Paste a Spotify episode URL, not a show URL."
                )
            target = await self._spotify_target(spotify.group(3))
            feeds = await self._search_feeds(target.show_title or target.title or "")
            episode = await self._select(target, feeds)
            return replace(episode, source_url=raw)

        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if parsed.scheme in {"http", "https"} and (
            host == "podcasts.apple.com" or host.endswith(".podcasts.apple.com")
        ):
            match = APPLE_ID_PATTERN.search(parsed.path)
            if not match:
                raise PodcastResolutionError("The Apple Podcasts URL is invalid.")
            episode_id = parse_qs(parsed.query).get("i", [None])[0]
            target, feeds = await self._apple_target(match.group(1), episode_id)
            episode = await self._select(target if episode_id else None, feeds)
            return replace(episode, source_url=raw)

        if parsed.scheme in {"http", "https"}:
            try:
                feed = await self._fetch_feed(raw)
                episode = max(
                    feed.episodes,
                    key=lambda item: (
                        item.published_at or datetime.min.replace(tzinfo=UTC)
                    ),
                )
            except PodcastResolutionError:
                feeds = await self._autodiscover(raw)
                episode = await self._select(EpisodeTarget(item_url=raw), feeds)
            return replace(episode, source_url=raw)

        target, feeds = await self._search_target_and_feeds(raw)
        return await self._select(target, feeds)


class PodcastService:
    def __init__(
        self,
        settings: Settings,
        assemblyai: AssemblyAITranscriber,
    ) -> None:
        self._settings = settings
        self._assemblyai = assemblyai
        self._resolver = PodcastResolver(settings)

    async def transcribe(self, value: str) -> TranscriptArtifact:
        episode = await self._resolver.resolve(value)
        if episode.duration_seconds is None or episode.duration_seconds <= 0:
            raise MediaLimitError(
                "The podcast feed did not provide a verifiable episode duration."
            )
        if episode.duration_seconds > self._settings.max_media_duration_seconds:
            raise MediaLimitError("The podcast episode exceeds the duration limit.")
        remote = await validate_remote_media(
            episode.enclosure_url,
            self._settings,
            declared_type=episode.enclosure_type,
            declared_length=episode.enclosure_length,
            allowed_prefixes=("audio/",),
        )
        transcript = await self._assemblyai.transcribe_url(remote.final_url)
        context = [f"Title: {episode.title}"]
        if episode.feed_title:
            context.append(f"Podcast: {episode.feed_title}")
        if episode.source_url:
            context.append(f"Source URL: {episode.source_url}")
        if episode.published_at:
            context.append(f"Published: {episode.published_at.isoformat()}")
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", episode.title).strip("-._")
        return TranscriptArtifact(
            title=episode.title,
            source_url=episode.source_url or episode.link,
            transcript=transcript,
            transcript_source="assemblyai",
            summary_context="\n".join(context),
            filename_stem=f"podcast-{safe_stem[:80] or 'episode'}",
            metadata={"Podcast": episode.feed_title or "Unknown"},
        )
