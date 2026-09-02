"""Console entry point."""

from __future__ import annotations

import shutil

from .bot import TranscriptionBot
from .config import get_settings
from .errors import ConfigurationError
from .logging_config import configure_logging


def _require_runtime_tools() -> None:
    missing = [
        name
        for name in ("ffmpeg", "ffprobe", "node", "yt-dlp")
        if shutil.which(name) is None
    ]
    if missing:
        raise ConfigurationError(
            f"Missing required runtime tools: {', '.join(missing)}"
        )


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    _require_runtime_tools()
    bot = TranscriptionBot(settings)
    bot.run(
        settings.discord_bot_token.get_secret_value(),
        log_handler=None,
    )


if __name__ == "__main__":
    main()
