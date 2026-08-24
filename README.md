# YouTube Transcription Bot

A standalone, security-first Discord bot that turns pasted YouTube and podcast inputs
or uploaded audio/video into a faithful summary plus a transcript attachment.

This repository is deliberately independent. It contains no private databases,
internal service URLs, webhooks, deployment hosts, archived data, external archive
exports, cookies, or credentials.

## Features

- Paste a YouTube URL into an allowlisted Discord channel.
- Try YouTube's timed transcript first, then yt-dlp subtitle extraction, then bounded
  yt-dlp audio download plus AssemblyAI only when captions are unavailable.
- Optional generic HTTP/HTTPS/SOCKS proxy support.
- Optional IPRoyal per-job sticky sessions with a fresh session identity for each
  retrieval attempt.
- Paste Spotify episode or Apple Podcasts episode URLs.
- Resolve direct RSS feeds and publisher pages with RSS autodiscovery.
- Search with `!podcast Show name | Episode title`.
- Upload audio or video directly to Discord for AssemblyAI transcription.
- Generate a source-faithful summary with the OpenAI Responses API.
- Return short summaries as a message, medium summaries as one embed, and oversized
  summaries as `summary.txt`; the transcript is always attached separately.
- Suppress Discord mentions at both generation and delivery time.
- Deduplicate repeated Discord message delivery for 15 minutes.

## Security and privacy defaults

- The bot processes messages only in channel IDs explicitly listed in
  `DISCORD_ALLOWED_CHANNEL_IDS`. An empty allowlist is a startup error.
- Messages from bots and webhooks are ignored, preventing reply loops.
- The bot opens no inbound port. It only makes outbound connections.
- Podcast and publisher URLs are protected against SSRF: only public HTTP(S)
  destinations on ports 80/443 are accepted, every DNS result and redirect is
  revalidated, DNS caching is disabled, and response sizes are bounded.
- The container runs as UID 10001 with a read-only root filesystem, no Linux
  capabilities, `no-new-privileges`, bounded PIDs/CPU/memory, and a temporary media
  filesystem.
- Proxy credentials are not placed in yt-dlp command arguments and provider error
  payloads are not included in default logs.
- If a configured proxy fails, the bot does **not** silently expose the host's direct
  IP. Direct fallback requires the explicit `YOUTUBE_ALLOW_DIRECT_FALLBACK=true`
  opt-in.
- `.env`, cookie files, databases, logs, caches, and editor state are excluded from
  Git and the Docker build context.

See [SECURITY.md](SECURITY.md) for reporting and threat-boundary details.

## Data sent to third parties

Running this software sends data to services you configure:

| Service | Data sent |
| --- | --- |
| Discord | Message content needed for routing; generated summary and transcript reply |
| YouTube / configured proxy | Video ID, transcript and subtitle requests, audio retrieval when fallback is needed |
| AssemblyAI | Downloaded YouTube audio, podcast enclosure URL, or uploaded media when transcription fallback is needed |
| OpenAI | Transcript text and minimal source metadata for summarization |
| Apple Podcasts | Podcast search or lookup terms |
| Spotify | Episode ID and configured market when a Spotify episode URL is used |
| PodcastIndex | Podcast search terms when credentials are configured |

Review those providers' terms and privacy policies. Only process media you are
authorized to access, download, and send to those services.

## Prerequisites

- A Discord bot token with the **Message Content Intent** enabled in the Discord
  developer portal.
- The bot invited with permission to view the allowlisted channel, read message
  history, send messages, embed links, attach files, and use external emojis only if
  your server requires them.
- An AssemblyAI API key.
- An OpenAI API key. The implementation uses the official Python client and the
  Responses API described in the [OpenAI Python API reference](https://developers.openai.com/api/reference/python/).
- Docker with Compose (recommended), or Python 3.11+, ffmpeg, Node.js, and yt-dlp.
- Optional Spotify client credentials for Spotify episode URLs.
- Optional PodcastIndex read credentials for broader feed search.
- Optional YouTube proxy credentials and a Netscape-format cookies file.

## Quick start with Docker

1. Clone the repository.
2. Copy `.env.example` to `.env`.
3. Populate the required keys and replace the example channel ID with the exact
   channel or comma-separated channel IDs the bot may read.
4. Start the outbound-only bot:

```bash
docker compose up -d --build
```

5. Verify the container reached Discord:

```bash
docker compose ps
docker compose logs --tail=100 bot
```

Do not paste `.env`, proxy URLs, Discord tokens, provider keys, signed podcast URLs,
or cookie contents into issues or logs.

## Inputs

In an allowlisted channel:

```text
https://www.youtube.com/watch?v=dQw4w9WgXcQ
https://open.spotify.com/episode/...
https://podcasts.apple.com/.../id...?i=...
!podcast https://example.com/feed.xml
!podcast Show name | Exact episode title
```

Audio and video attachments are also accepted. When a message contains multiple
supported inputs, the first supported URL is processed; otherwise the first supported
attachment is used. Send separate messages for separate jobs.

For a generic publisher page or direct RSS URL, use the `!podcast` prefix. Automatic
URL routing is intentionally limited to YouTube, Spotify, and Apple hosts so ordinary
links in an allowlisted channel are not fetched unexpectedly.

## Proxy configuration

Set `YOUTUBE_PROXY_ENABLED=true` and provide the host, port, protocol, and optional
username/password fields. Do not embed credentials in `YOUTUBE_PROXY_HOST`.

For IPRoyal, use:

```dotenv
YOUTUBE_PROXY_PROVIDER=iproyal
YOUTUBE_PROXY_HOST=geo.iproyal.com
YOUTUBE_PROXY_PORT=12321
YOUTUBE_PROXY_USERNAME=...
YOUTUBE_PROXY_PASSWORD=...
YOUTUBE_PROXY_COUNTRY=us
YOUTUBE_PROXY_SESSION_LIFETIME=10m
```

The password, country, and a random eight-character session ID are composed only in
memory. The completed credential is never logged or placed in a child process's
argument list.

## Optional YouTube cookies

Cookies can improve access to age-restricted or bot-protected videos, but they grant
the privileges of the exporting account and are sensitive. Keep the file outside the
repository and mount it read-only. Then set `YOUTUBE_COOKIES_FILE` to its path inside
the container. The provided Compose file does not mount cookies automatically.

Example override:

```yaml
services:
  bot:
    volumes:
      - type: bind
        source: /absolute/private/path/cookies.txt
        target: /run/secrets/youtube-cookies.txt
        read_only: true
        bind:
          create_host_path: false
```

Then set `YOUTUBE_COOKIES_FILE=/run/secrets/youtube-cookies.txt` in `.env`.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
pytest
ruff check .
mypy
python -m youtube_transcription_bot
```

The test suite is offline and uses no real provider credentials. Before deployment,
perform a private smoke test in a dedicated Discord channel using test/provider keys;
never place live credentials in test fixtures.

## Limitations

- Spotify show URLs are intentionally rejected; use a specific episode URL.
- Ambiguous podcast searches fail closed. Add both show and episode separated by `|`.
- Live/upcoming YouTube videos are not treated as completed media.
- Very long or large media is rejected at configured limits rather than truncated.
- Podcast feeds must publish a non-zero enclosure size and episode duration so the
  bot can enforce limits before sending the media URL to AssemblyAI.
- This project does not bypass DRM, paywalls, access controls, or provider terms.

## License

MIT. See [LICENSE](LICENSE).
