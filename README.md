# YouTube Transcription Bot

A standalone Discord bot that turns YouTube videos, podcast episodes, and uploaded
audio/video into a source-faithful summary plus a transcript attachment. It reads
only explicitly allowed channels and replies to the source message; it does not relay
messages between servers or use webhooks.

## Setup with Docker

Docker with Compose is recommended. Startup requires four environment values: a
Discord bot token, one or more allowed channel IDs, an AssemblyAI API key, and an
OpenAI API key. AssemblyAI is required even if you expect YouTube captions to exist.

### 1. Create and invite the Discord bot

1. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application and bot. Under **Bot**, enable **Message Content Intent**.
2. Under **OAuth2 > URL Generator**, select the `bot` scope and these permissions:
   **View Channels**, **Read Message History**, **Send Messages**, **Embed Links**,
   and **Attach Files**. Open the generated URL and invite the bot to your server.
3. In Discord, enable **User Settings > Advanced > Developer Mode**, then right-click
   each channel the bot may read and select **Copy Channel ID**.

Use the bot token from the developer portal—never a Discord user token.

### 2. Configure the service

```bash
git clone https://github.com/ToshiGGs/Youtube-Transcription.git
cd Youtube-Transcription
cp .env.example .env
```

Set these values in `.env`; separate multiple channel IDs with commas:

```dotenv
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_CHANNEL_IDS=123456789012345678
ASSEMBLYAI_API_KEY=...
OPENAI_API_KEY=...
```

Create provider keys in the [AssemblyAI dashboard](https://www.assemblyai.com/dashboard/)
and [OpenAI API platform](https://platform.openai.com/api-keys). Keep `.env`
private; all other settings in `.env.example` are optional or have safe defaults.

### 3. Start and test

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 bot
```

Wait for `Discord bot is connected and ready`, then paste a YouTube URL in an allowed
channel. The bot should reply with a summary and a `.txt` transcript. It opens no
inbound port.

## Supported inputs

| Input | Send in an allowed channel |
| --- | --- |
| YouTube video | A `youtube.com` or `youtu.be` URL |
| Spotify episode | An episode URL; requires Spotify credentials |
| Apple Podcasts | An episode URL; a show URL selects its latest episode |
| RSS feed | `!podcast https://example.com/feed.xml` selects the latest episode |
| Publisher episode page | `!podcast URL`; the page must expose RSS and match a feed item |
| Podcast search | `!podcast Show name \| Exact episode title` |
| Upload | Attach a supported audio or video file |

Automatic URL routing is limited to YouTube, Spotify, and Apple Podcasts. Generic
URLs require `!podcast`, preventing ordinary links from being fetched unexpectedly.
If a message has several inputs, only the first supported URL—or otherwise the first
supported attachment—is processed.

## Processing behavior

- YouTube tries a timed transcript, yt-dlp subtitles, then bounded audio download
  with AssemblyAI.
- Podcasts resolve to one public RSS episode and use AssemblyAI. Ambiguous searches
  fail closed; use the `Show | Episode` form.
- Uploaded video is converted to audio with ffmpeg. OpenAI then summarizes every
  transcript; replies suppress mentions and include transcript provenance.

## Optional integrations

- Spotify URLs need both `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`.
- PodcastIndex adds a search source and needs both `PODCAST_INDEX_API_KEY` and
  `PODCAST_INDEX_API_SECRET`. Apple Podcasts and direct RSS need no credentials.

For a YouTube proxy, set `YOUTUBE_PROXY_ENABLED=true` plus the host, port, protocol,
and optional credentials shown in `.env.example`; never embed credentials in the
host. Direct fallback requires `YOUTUBE_ALLOW_DIRECT_FALLBACK=true`. IPRoyal mode
requires `YOUTUBE_PROXY_PROVIDER=iproyal`; it uses a fresh sticky identity per job
and rotates it for later fallback profiles or confirmed proxy failures.

For age-restricted or bot-protected videos, keep a Netscape-format cookie file
outside the repository and mount it read-only with a Compose override:

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

## Limits and security

- Defaults: 500 MiB per upload, 512 MiB per remote podcast file, six hours of media,
  and 1.5 million transcript characters.
- YouTube completion and duration checks apply only when audio fallback is needed;
  caption paths run first.
- Podcasts require a feed duration and a non-zero size from RSS or HTTP
  `Content-Length`/`Content-Range`.
- Bots and webhooks are ignored. Podcast requests reject private networks, URL
  credentials, nonstandard ports, unsafe redirects, oversized responses, and wrong
  media types.
- Compose runs non-root with a read-only filesystem, dropped capabilities,
  `no-new-privileges`, resource limits, and temporary media storage.
- The bot does not bypass DRM, paywalls, access controls, or provider terms.

Data flow: Discord receives the input and reply; OpenAI receives transcript text and
minimal source context with `store=false`; AssemblyAI receives media or a validated
podcast URL when needed. Input providers and any configured proxy receive their
required lookup requests. Process only media you may use.

Secrets, cookies, databases, logs, and caches are excluded from Git and Docker build
context. See [SECURITY.md](SECURITY.md) for details.

## Development

Python 3.11 or 3.12 plus ffmpeg, ffprobe, Node.js, and yt-dlp are required outside
Docker.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
pytest
ruff check .
mypy
```

Tests are offline and use fake credentials. Perform the first live test in a private
Discord channel.

## License

MIT. See [LICENSE](LICENSE).
