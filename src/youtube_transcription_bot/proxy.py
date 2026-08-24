"""Per-job YouTube proxy identities without credential disclosure."""

from __future__ import annotations

import os
import secrets
import string
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from pydantic import SecretStr

from .config import Settings
from .errors import ConfigurationError

DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
SESSION_ID_LENGTH = 8
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
TRANSPORT_ERROR_MARKERS = (
    "unable to connect to proxy",
    "tunnel connection failed",
    "proxy connection failed",
    "proxy connect",
    "proxy transport",
    "proxy timed out",
    "proxy timeout",
    "proxyerror",
)
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
)


@dataclass(frozen=True)
class YouTubeRequestIdentity:
    proxy_url: SecretStr | None
    user_agent: str
    accept_language: str = DEFAULT_ACCEPT_LANGUAGE

    @property
    def proxy_value(self) -> str | None:
        return self.proxy_url.get_secret_value() if self.proxy_url else None

    @property
    def ytdlp_args(self) -> list[str]:
        # Proxy credentials stay in the child environment rather than argv so
        # they are not exposed by process-listing tools.
        return [
            "--add-headers",
            f"User-Agent:{self.user_agent}",
            "--add-headers",
            f"Accept-Language:{self.accept_language}",
        ]

    @property
    def requests_proxies(self) -> dict[str, str] | None:
        value = self.proxy_value
        return {"http": value, "https": value} if value else None

    @property
    def subprocess_env(self) -> dict[str, str]:
        env = direct_subprocess_env()
        value = self.proxy_value
        if value:
            scheme = urlsplit(value).scheme.lower()
            env.update(
                {
                    "HTTP_PROXY": value,
                    "HTTPS_PROXY": value,
                    "http_proxy": value,
                    "https_proxy": value,
                }
            )
            if scheme.startswith("socks"):
                env["ALL_PROXY"] = value
                env["all_proxy"] = value
            env.pop("NO_PROXY", None)
            env.pop("no_proxy", None)
        return env


def direct_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in PROXY_ENV_KEYS:
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def _session_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(SESSION_ID_LENGTH))


def _proxy_url(settings: Settings) -> SecretStr:
    raw_host = settings.youtube_proxy_host.strip()
    parsed = urlsplit(
        raw_host
        if "://" in raw_host
        else f"{settings.youtube_proxy_protocol}://{raw_host}"
    )
    if not parsed.hostname:
        raise ConfigurationError("The configured YouTube proxy host is invalid.")
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise ConfigurationError(
            "Put proxy credentials in their dedicated settings, not in the host field."
        )
    if parsed.query or parsed.fragment:
        raise ConfigurationError("The configured YouTube proxy host is invalid.")

    username = settings.youtube_proxy_username.strip()
    password = settings.youtube_proxy_password.get_secret_value().strip()
    if settings.youtube_proxy_provider == "iproyal":
        tags: list[str] = []
        if settings.youtube_proxy_country:
            tags.append(f"country-{settings.youtube_proxy_country}")
        tags.extend(
            [
                f"session-{_session_id()}",
                f"lifetime-{settings.youtube_proxy_session_lifetime}",
            ]
        )
        password = f"{password}_{'_'.join(tags)}"
    auth = ""
    if username or password:
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    port = parsed.port or settings.youtube_proxy_port
    netloc = f"{auth}{parsed.hostname}:{port}"
    return SecretStr(
        urlunsplit(
            (parsed.scheme or settings.youtube_proxy_protocol, netloc, "", "", "")
        )
    )


def build_youtube_identity(
    settings: Settings, *, direct: bool = False
) -> YouTubeRequestIdentity:
    proxy_url = None
    if settings.youtube_proxy_enabled and not direct:
        proxy_url = _proxy_url(settings)
    return YouTubeRequestIdentity(
        proxy_url=proxy_url,
        user_agent=secrets.choice(USER_AGENTS),
    )


def is_proxy_transport_error(error: BaseException | str) -> bool:
    if isinstance(error, requests.exceptions.ProxyError):
        return True
    if isinstance(error, BaseException):
        parts = (
            str(getattr(error, "stdout", "") or ""),
            str(getattr(error, "stderr", "") or ""),
            str(error),
        )
        text = " ".join(parts).lower()
    else:
        text = error.lower()
    return any(marker in text for marker in TRANSPORT_ERROR_MARKERS)
