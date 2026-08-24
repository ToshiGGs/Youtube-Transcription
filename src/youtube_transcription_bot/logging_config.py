"""Logging that strips credentials and URL user information."""

from __future__ import annotations

import logging
import re

from .config import Settings

URL_AUTH_PATTERN = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@")
TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"\b(?:mfa\.)?[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b"
    ),
)


def redact_text(value: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(value)
    text = URL_AUTH_PATTERN.sub(r"\1[REDACTED]@", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.msg, self._secrets)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact_text(value, self._secrets)
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    redact_text(value, self._secrets) for value in record.args
                )
        return True


class RedactingFormatter(logging.Formatter):
    """Redact the final rendered record, including exception tracebacks."""

    def __init__(self, fmt: str, secrets: tuple[str, ...]) -> None:
        super().__init__(fmt)
        self._secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record), self._secrets)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter(settings.secret_values))
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            settings.secret_values,
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)
