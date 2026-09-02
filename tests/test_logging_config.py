from __future__ import annotations

import logging
import sys

from media_transcription_bot.logging_config import RedactingFormatter


def test_formatter_redacts_secrets_in_tracebacks() -> None:
    known_value = "sensitive-provider-token"
    try:
        raise RuntimeError(f"provider failed with {known_value}")
    except RuntimeError:
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="request failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    rendered = RedactingFormatter("%(message)s", (known_value,)).format(record)

    assert known_value not in rendered
    assert "[REDACTED]" in rendered
