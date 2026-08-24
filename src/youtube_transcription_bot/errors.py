"""Exceptions whose messages are safe to show to Discord users."""


class UserVisibleError(RuntimeError):
    """A bounded, credential-free failure message suitable for Discord."""


class ConfigurationError(UserVisibleError):
    """Startup configuration is missing or unsafe."""


class UnsupportedInputError(UserVisibleError):
    """The message did not contain a supported media input."""


class UnsafeRemoteUrlError(UserVisibleError):
    """A remote URL failed the public-network safety policy."""


class MediaLimitError(UserVisibleError):
    """A media item exceeded a configured resource bound."""


class TranscriptUnavailableError(UserVisibleError):
    """No supported transcript path produced usable text."""


class PodcastResolutionError(UserVisibleError):
    """A podcast input could not be resolved to one public RSS episode."""
