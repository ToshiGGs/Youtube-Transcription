"""Docker health probe for the Discord-ready heartbeat."""

from __future__ import annotations

from .heartbeat import heartbeat_age

MAX_HEARTBEAT_AGE_SECONDS = 120


def main() -> None:
    try:
        age = heartbeat_age()
    except (OSError, UnicodeDecodeError, ValueError):
        raise SystemExit(1) from None
    raise SystemExit(0 if 0 <= age <= MAX_HEARTBEAT_AGE_SECONDS else 1)


if __name__ == "__main__":
    main()
