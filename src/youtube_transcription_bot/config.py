"""Environment-only configuration with fail-closed validation."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigurationError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    discord_bot_token: SecretStr
    discord_allowed_channel_ids: frozenset[int]
    assemblyai_api_key: SecretStr
    openai_api_key: SecretStr
    openai_model: str = "gpt-5.5"

    summary_chunk_chars: int = Field(default=30_000, ge=8_000, le=100_000)
    max_transcript_chars: int = Field(default=1_500_000, ge=30_000, le=5_000_000)

    spotify_client_id: str = ""
    spotify_client_secret: SecretStr = SecretStr("")
    spotify_market: str = "US"
    podcast_index_api_key: str = ""
    podcast_index_api_secret: SecretStr = SecretStr("")

    youtube_proxy_enabled: bool = False
    youtube_proxy_provider: Literal["generic", "iproyal"] = "generic"
    youtube_proxy_protocol: Literal["http", "https", "socks5"] = "http"
    youtube_proxy_host: str = ""
    youtube_proxy_port: int | None = Field(default=None, ge=1, le=65535)
    youtube_proxy_username: str = ""
    youtube_proxy_password: SecretStr = SecretStr("")
    youtube_proxy_country: str = "us"
    youtube_proxy_session_lifetime: str = "10m"
    youtube_allow_direct_fallback: bool = False
    youtube_cookies_file: Path | None = None

    max_concurrent_jobs: int = Field(default=1, ge=1, le=8)
    max_attachment_bytes: int = Field(
        default=500 * 1024 * 1024,
        ge=1 * 1024 * 1024,
        le=2 * 1024 * 1024 * 1024,
    )
    max_remote_media_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1 * 1024 * 1024,
        le=2 * 1024 * 1024 * 1024,
    )
    max_metadata_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=64 * 1024,
        le=50 * 1024 * 1024,
    )
    max_media_duration_seconds: int = Field(default=6 * 60 * 60, ge=60, le=24 * 60 * 60)
    remote_request_timeout_seconds: int = Field(default=30, ge=5, le=120)
    remote_max_redirects: int = Field(default=5, ge=0, le=10)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("discord_allowed_channel_ids", mode="before")
    @classmethod
    def parse_channel_ids(cls, value: object) -> object:
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
            try:
                return frozenset(int(part) for part in parts)
            except ValueError as exc:
                raise ValueError(
                    "DISCORD_ALLOWED_CHANNEL_IDS must be comma-separated integers"
                ) from exc
        return value

    @field_validator("spotify_market")
    @classmethod
    def normalize_market(cls, value: str) -> str:
        market = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", market):
            raise ValueError("SPOTIFY_MARKET must be a two-letter country code")
        return market

    @field_validator("youtube_proxy_country")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        country = value.strip().lower()
        if country and not re.fullmatch(r"[a-z]{2}", country):
            raise ValueError("YOUTUBE_PROXY_COUNTRY must be a two-letter country code")
        return country

    @field_validator("youtube_proxy_session_lifetime")
    @classmethod
    def validate_session_lifetime(cls, value: str) -> str:
        lifetime = value.strip().lower()
        if not re.fullmatch(r"\d+[smhd]", lifetime):
            raise ValueError(
                "YOUTUBE_PROXY_SESSION_LIFETIME must use s, m, h, or d units"
            )
        return lifetime

    @model_validator(mode="after")
    def validate_security_contract(self) -> Settings:
        if not self.discord_allowed_channel_ids:
            raise ValueError(
                "DISCORD_ALLOWED_CHANNEL_IDS must explicitly allow at least one channel"
            )
        required_secrets = {
            "DISCORD_BOT_TOKEN": self.discord_bot_token,
            "ASSEMBLYAI_API_KEY": self.assemblyai_api_key,
            "OPENAI_API_KEY": self.openai_api_key,
        }
        missing = [
            name
            for name, secret in required_secrets.items()
            if not secret.get_secret_value().strip()
        ]
        if missing:
            raise ValueError(f"Missing required settings: {', '.join(missing)}")

        spotify_secret = self.spotify_client_secret.get_secret_value().strip()
        if bool(self.spotify_client_id.strip()) != bool(spotify_secret):
            raise ValueError(
                "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set together"
            )
        podcast_secret = self.podcast_index_api_secret.get_secret_value().strip()
        if bool(self.podcast_index_api_key.strip()) != bool(podcast_secret):
            raise ValueError(
                "PODCAST_INDEX_API_KEY and PODCAST_INDEX_API_SECRET "
                "must be set together"
            )

        if self.youtube_proxy_enabled:
            if not self.youtube_proxy_host.strip() or self.youtube_proxy_port is None:
                raise ValueError(
                    "A proxy host and port are required when YOUTUBE_PROXY_ENABLED=true"
                )
            if self.youtube_proxy_provider == "iproyal" and (
                not self.youtube_proxy_username.strip()
                or not self.youtube_proxy_password.get_secret_value().strip()
            ):
                raise ValueError("IPRoyal proxy mode requires a username and password")

        if self.youtube_cookies_file is not None:
            path = self.youtube_cookies_file.expanduser().resolve()
            if not path.is_file():
                raise ValueError("YOUTUBE_COOKIES_FILE must point to an existing file")
            self.youtube_cookies_file = path
        return self

    @property
    def spotify_enabled(self) -> bool:
        return bool(
            self.spotify_client_id.strip()
            and self.spotify_client_secret.get_secret_value().strip()
        )

    @property
    def podcast_index_enabled(self) -> bool:
        return bool(
            self.podcast_index_api_key.strip()
            and self.podcast_index_api_secret.get_secret_value().strip()
        )

    @property
    def secret_values(self) -> tuple[str, ...]:
        candidates = (
            self.discord_bot_token.get_secret_value(),
            self.assemblyai_api_key.get_secret_value(),
            self.openai_api_key.get_secret_value(),
            self.spotify_client_secret.get_secret_value(),
            self.podcast_index_api_secret.get_secret_value(),
            self.youtube_proxy_password.get_secret_value(),
        )
        return tuple(value for value in candidates if value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:
        raise ConfigurationError("Configuration validation failed.") from exc
