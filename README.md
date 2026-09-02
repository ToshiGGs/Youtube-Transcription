# Media Transcription & Summary Bot

A self-hosted Discord bot that turns YouTube videos, podcast episodes, and uploaded
audio or video into two useful outputs: a source-faithful summary and a downloadable
transcript. It watches only the Discord channels you explicitly allow and replies to
the original message. It does not relay messages between servers or use webhooks.

## What each piece does

| Piece | Purpose |
| --- | --- |
| Discord | The inbox and delivery interface. People post media; the bot replies with the result. |
| This bot | Recognizes the input, obtains or creates a transcript, requests a summary, and safely returns both. |
| YouTube transcript tools | Try captions first, then subtitles, then an audio fallback. |
| AssemblyAI | Converts audio to text when a usable transcript is not already available. |
| OpenAI | Creates the summary from the transcript; it does not receive your Discord token. |
| ffmpeg and yt-dlp | Extract or download media when transcription requires audio. |
| Docker Compose | Runs the service with its dependencies and security limits. No inbound port is opened. |

AssemblyAI and OpenAI are both required. Spotify, PodcastIndex, a YouTube proxy, and
YouTube cookies are optional and are only needed for the matching features described
below.

## Set up with an AI coding agent

The intended setup path is AI-assisted. You gather the accounts, keys, and Discord
channel IDs; the coding agent checks the machine, prepares the root configuration,
starts the service, and explains any failure. Do not paste secrets into an AI chat.

### What the human needs to collect

1. Install Docker with Compose, or ask your coding agent to check whether it is
   already available.
2. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application and bot. Under **Bot**, enable **Message Content Intent**.
3. Under **OAuth2 > URL Generator**, select the `bot` scope and these permissions:
   **View Channels**, **Read Message History**, **Send Messages**, **Embed Links**,
   and **Attach Files**. Open the generated URL and invite the bot to your server.
4. In Discord, enable **User Settings > Advanced > Developer Mode**. Right-click each
   channel the bot may read and select **Copy Channel ID**.
5. Create keys in the [AssemblyAI dashboard](https://www.assemblyai.com/dashboard/)
   and [OpenAI API platform](https://platform.openai.com/api-keys).

Use the Discord bot token from the developer portal—never a Discord user token.

### Give this prompt to the coding agent

Open this repository in your coding agent, then send it this prompt:

```text
Set up and verify this repository locally with Docker Compose.

Read README.md, .env.example, compose.yaml, and the relevant configuration code
before acting. Preserve the existing security defaults and do not install or upgrade
global packages.

If the repository-root .env file does not exist, copy .env.example to .env. Never
print, log, commit, or paste secret values. Stop after creating the file and tell me
only which required variable names I must fill in.

After I confirm that .env is ready, validate the configuration without displaying
its contents. Then run the repository's Docker Compose preflight, build and start the
bot, check service health, and inspect bounded recent logs. Report the exact check
results, explain any failure in plain English, and tell me how to run one private
Discord smoke test. Do not change application behavior just to make startup pass.
```

The agent will create `.env` as a **file** in the repository root. Paste these four
values into that file yourself; separate multiple channel IDs with commas:

```dotenv
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_CHANNEL_IDS=123456789012345678
ASSEMBLYAI_API_KEY=...
OPENAI_API_KEY=...
```

All other values in `.env.example` are optional or have defaults. Keep `.env`
private. Once it is filled in, tell the agent: `The root .env file is ready; continue
the setup and verification.`

A successful setup ends with a healthy `bot` service and the log message
`Discord bot is connected and ready`. For the private smoke test, post a YouTube URL
in one allowed channel. The bot should reply with a summary and a `.txt` transcript.

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
