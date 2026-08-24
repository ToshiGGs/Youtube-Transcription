"""SSRF-resistant remote HTTP helpers for podcast metadata and media."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Collection
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

from .config import Settings
from .errors import MediaLimitError, UnsafeRemoteUrlError
from .models import RemoteMedia

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
HEAD_FALLBACK_STATUSES = frozenset({400, 403, 405})
GENERIC_MEDIA_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})
PUBLIC_USER_AGENT = "YoutubeTranscriptionBot/1.0"


def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def validate_remote_url_shape(
    url: str,
    *,
    label: str = "Remote URL",
    allowed_ports: Collection[int] = (80, 443),
) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeRemoteUrlError(f"{label} must use HTTP or HTTPS.")
    if not parsed.hostname:
        raise UnsafeRemoteUrlError(f"{label} must include a host.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeRemoteUrlError(f"{label} must not include URL credentials.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeRemoteUrlError(f"{label} includes an invalid port.") from exc
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    if effective_port not in allowed_ports:
        raise UnsafeRemoteUrlError(f"{label} must use a standard HTTP or HTTPS port.")
    if is_public_ip(parsed.hostname):
        return
    try:
        literal_ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not literal_ip.is_global:
        raise UnsafeRemoteUrlError(f"{label} points to a non-public address.")


class PublicIPResolver(AbstractResolver):
    """Only return globally routable records, preventing DNS rebinding to LANs."""

    def __init__(self, resolver: AbstractResolver | None = None) -> None:
        self._resolver = resolver or aiohttp.resolver.ThreadedResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        records = await self._resolver.resolve(host, port, family)
        public_records = [
            record
            for record in records
            if isinstance(record.get("host"), str) and is_public_ip(str(record["host"]))
        ]
        if not public_records:
            raise UnsafeRemoteUrlError(
                "The remote host resolved to no public IP addresses."
            )
        return public_records

    async def close(self) -> None:
        await self._resolver.close()


def create_public_session(settings: Settings) -> aiohttp.ClientSession:
    timeout = aiohttp.ClientTimeout(total=settings.remote_request_timeout_seconds)
    connector = aiohttp.TCPConnector(
        resolver=PublicIPResolver(),
        use_dns_cache=False,
    )
    return aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=False,
        headers={"User-Agent": PUBLIC_USER_AGENT},
    )


async def validate_public_host(url: str, *, label: str = "Remote URL") -> None:
    validate_remote_url_shape(url, label=label)
    hostname = urlsplit(url).hostname
    if hostname is None:
        raise UnsafeRemoteUrlError(f"{label} must include a host.")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(hostname, None)
    except OSError as exc:
        raise UnsafeRemoteUrlError("The remote host could not be resolved.") from exc
    if not addresses or any(not is_public_ip(item[4][0]) for item in addresses):
        raise UnsafeRemoteUrlError("The remote host resolved to a non-public address.")


def _parse_length(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _parse_range_total(value: str | None) -> int | None:
    if not value or "/" not in value:
        return None
    total = value.rsplit("/", maxsplit=1)[-1]
    return None if total == "*" else _parse_length(total)


async def read_limited_text(
    response: aiohttp.ClientResponse,
    max_bytes: int,
    *,
    label: str,
) -> str:
    declared = _parse_length(response.headers.get("Content-Length"))
    if declared is not None and declared > max_bytes:
        raise MediaLimitError(f"{label} exceeds the configured size limit.")
    chunks: list[bytes] = []
    bytes_read = 0
    while True:
        chunk = await response.content.read(min(64 * 1024, max_bytes - bytes_read + 1))
        if not chunk:
            break
        bytes_read += len(chunk)
        if bytes_read > max_bytes:
            raise MediaLimitError(f"{label} exceeds the configured size limit.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        return raw.decode(response.charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


async def fetch_public_text(
    url: str,
    settings: Settings,
    *,
    label: str,
    accept: str,
) -> tuple[str, str, str | None]:
    current_url = url
    redirects = 0
    async with create_public_session(settings) as session:
        while True:
            await validate_public_host(current_url, label=label)
            async with session.get(
                URL(current_url, encoded=True),
                allow_redirects=False,
                headers={"Accept": accept},
            ) as response:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise UnsafeRemoteUrlError(
                            f"{label} redirected without a destination."
                        )
                    redirects += 1
                    if redirects > settings.remote_max_redirects:
                        raise UnsafeRemoteUrlError(
                            f"{label} exceeded the redirect limit."
                        )
                    current_url = urljoin(str(response.url), location)
                    continue
                if not 200 <= response.status < 300:
                    raise UnsafeRemoteUrlError(
                        f"{label} returned HTTP {response.status}."
                    )
                text = await read_limited_text(
                    response,
                    settings.max_metadata_bytes,
                    label=label,
                )
                return text, str(response.url), response.headers.get("Content-Type")


def _supported_media_type(
    response_type: str | None,
    declared_type: str | None,
    allowed_prefixes: Collection[str],
) -> bool:
    def normalize(value: str | None) -> str:
        return (value or "").split(";", maxsplit=1)[0].strip().lower()

    def allowed(value: str) -> bool:
        return value in GENERIC_MEDIA_TYPES or any(
            value.startswith(prefix) for prefix in allowed_prefixes
        )

    response_value = normalize(response_type)
    declared_value = normalize(declared_type)
    if response_value:
        # An explicit HTTP response type is authoritative. A feed's enclosure
        # declaration must not override a server that returned HTML or another
        # unsupported payload.
        if response_value in GENERIC_MEDIA_TYPES:
            return bool(
                declared_value
                and declared_value not in GENERIC_MEDIA_TYPES
                and allowed(declared_value)
            )
        return allowed(response_value)
    return bool(
        declared_value
        and declared_value not in GENERIC_MEDIA_TYPES
        and allowed(declared_value)
    )


def _validate_media_response(
    *,
    final_url: str,
    response_type: str | None,
    response_length: int | None,
    declared_type: str | None,
    declared_length: int | None,
    settings: Settings,
    allowed_prefixes: Collection[str],
) -> RemoteMedia:
    if not _supported_media_type(response_type, declared_type, allowed_prefixes):
        raise UnsafeRemoteUrlError(
            "The remote enclosure did not report a supported media type."
        )
    lengths = [
        value
        for value in (response_length, declared_length)
        if value is not None and value >= 0
    ]
    content_length = max(lengths) if lengths else None
    if content_length is None or content_length <= 0:
        raise MediaLimitError(
            "The remote media did not provide a verifiable non-zero size."
        )
    if content_length is not None and content_length > settings.max_remote_media_bytes:
        raise MediaLimitError("The remote media exceeds the configured size limit.")
    return RemoteMedia(
        final_url=final_url,
        content_type=response_type or declared_type,
        content_length=content_length,
    )


async def validate_remote_media(
    url: str,
    settings: Settings,
    *,
    declared_type: str | None = None,
    declared_length: int | None = None,
    allowed_prefixes: Collection[str] = ("audio/",),
) -> RemoteMedia:
    current_url = url
    redirects = 0
    async with create_public_session(settings) as session:
        while True:
            await validate_public_host(current_url, label="Podcast media URL")
            async with session.head(
                URL(current_url, encoded=True),
                allow_redirects=False,
                headers={"Accept": "*/*"},
            ) as response:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise UnsafeRemoteUrlError(
                            "Podcast media redirected without a destination."
                        )
                    redirects += 1
                    if redirects > settings.remote_max_redirects:
                        raise UnsafeRemoteUrlError(
                            "Podcast media exceeded the redirect limit."
                        )
                    current_url = urljoin(str(response.url), location)
                    continue
                if response.status in HEAD_FALLBACK_STATUSES or response.status >= 500:
                    break
                if not 200 <= response.status < 300:
                    raise UnsafeRemoteUrlError(
                        f"Podcast media returned HTTP {response.status}."
                    )
                return _validate_media_response(
                    final_url=str(response.url),
                    response_type=response.headers.get("Content-Type"),
                    response_length=_parse_length(
                        response.headers.get("Content-Length")
                    ),
                    declared_type=declared_type,
                    declared_length=declared_length,
                    settings=settings,
                    allowed_prefixes=allowed_prefixes,
                )

        while True:
            await validate_public_host(current_url, label="Podcast media URL")
            async with session.get(
                URL(current_url, encoded=True),
                allow_redirects=False,
                headers={"Accept": "*/*", "Range": "bytes=0-0"},
            ) as response:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise UnsafeRemoteUrlError(
                            "Podcast media redirected without a destination."
                        )
                    redirects += 1
                    if redirects > settings.remote_max_redirects:
                        raise UnsafeRemoteUrlError(
                            "Podcast media exceeded the redirect limit."
                        )
                    current_url = urljoin(str(response.url), location)
                    continue
                if not 200 <= response.status < 300:
                    raise UnsafeRemoteUrlError(
                        f"Podcast media returned HTTP {response.status}."
                    )
                length = (
                    _parse_range_total(response.headers.get("Content-Range"))
                    if response.status == 206
                    else _parse_length(response.headers.get("Content-Length"))
                )
                return _validate_media_response(
                    final_url=str(response.url),
                    response_type=response.headers.get("Content-Type"),
                    response_length=length,
                    declared_type=declared_type,
                    declared_length=declared_length,
                    settings=settings,
                    allowed_prefixes=allowed_prefixes,
                )
