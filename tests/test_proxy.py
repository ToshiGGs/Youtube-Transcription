from __future__ import annotations

from youtube_transcription_bot.proxy import build_youtube_identity


def test_proxy_credentials_never_enter_ytdlp_arguments(settings_factory):
    settings = settings_factory(
        youtube_proxy_enabled=True,
        youtube_proxy_provider="generic",
        youtube_proxy_host="proxy.example",
        youtube_proxy_port=8080,
        youtube_proxy_username="proxy-user",
        youtube_proxy_password="proxy-password-not-real",  # noqa: S106
    )
    identity = build_youtube_identity(settings)
    arguments = " ".join(identity.ytdlp_args)
    assert "proxy-user" not in arguments
    assert "proxy-password-not-real" not in arguments
    assert "proxy-password-not-real" not in repr(identity)
    assert identity.subprocess_env["HTTPS_PROXY"].startswith("http://")


def test_iproyal_identity_rotates_per_job(settings_factory):
    settings = settings_factory(
        youtube_proxy_enabled=True,
        youtube_proxy_provider="iproyal",
        youtube_proxy_host="geo.iproyal.com",
        youtube_proxy_port=12321,
        youtube_proxy_username="proxy-user",
        youtube_proxy_password="proxy-password-not-real",  # noqa: S106
        youtube_proxy_country="us",
        youtube_proxy_session_lifetime="10m",
    )
    first = build_youtube_identity(settings).proxy_value
    second = build_youtube_identity(settings).proxy_value
    assert first != second
    assert "country-us" in (first or "")
    assert "lifetime-10m" in (first or "")


def test_direct_identity_contains_no_proxy(settings_factory):
    settings = settings_factory(
        youtube_proxy_enabled=True,
        youtube_proxy_host="proxy.example",
        youtube_proxy_port=8080,
    )
    assert build_youtube_identity(settings, direct=True).proxy_value is None
