# Media Transcription Bot

Media Transcription Bot is a self-hosted Discord bot for turning media into useful
notes. Send it a YouTube video, podcast episode, or audio/video file and it will
reply with a clear summary and a downloadable transcript.

The bot only watches the Discord channels you choose. It replies in the same
channel, does not relay messages to other servers, and does not use webhooks.

## How it works

When you share a YouTube link, the bot looks for an existing transcript first. If
one is not available, it tries YouTube subtitles and then downloads the audio for
transcription with AssemblyAI. Podcast episodes and uploaded files are also
transcribed with AssemblyAI. Once the transcript is ready, OpenAI writes the
summary and the bot replies with that summary and a transcript file.

AssemblyAI and OpenAI are required. Spotify, PodcastIndex, a YouTube proxy, and
YouTube cookies are optional; you only need them if you want the matching feature.

## The easiest way to set it up

This project is designed to be set up with an AI coding agent. Your part is to
create the necessary accounts, collect four values, and paste them into a private
`.env` file. The agent can check Docker, prepare the file, start the bot, and verify
that it is healthy.

Do not paste API keys or bot tokens into an AI chat.

### 1. Create your Discord bot

Go to the [Discord Developer Portal](https://discord.com/developers/applications)
and create an application. Add a bot to the application, then open the **Bot** page
and enable **Message Content Intent**.

Next, open **OAuth2 > URL Generator**. Select the `bot` scope and give it these
permissions:

- View Channels
- Read Message History
- Send Messages
- Embed Links
- Attach Files

Open the generated URL to invite the bot to your Discord server.

You also need the ID of every channel where the bot should work. In Discord, enable
**User Settings > Advanced > Developer Mode**, then right-click each channel and
choose **Copy Channel ID**.

Finally, copy the bot token from the Discord developer portal. Use the bot token,
never a Discord user token.

### 2. Create the two API keys

Create one key in the [AssemblyAI dashboard](https://www.assemblyai.com/dashboard/)
and one in the [OpenAI API platform](https://platform.openai.com/api-keys).

At this point you should have:

- a Discord bot token
- one or more allowed Discord channel IDs
- an AssemblyAI API key
- an OpenAI API key

### 3. Let an AI coding agent handle the setup

Open this repository in your coding agent and give it the following prompt:

```text
Set up this project locally with Docker Compose and verify that it works.

Read README.md, .env.example, compose.yaml, and the configuration code first.
Preserve the existing security settings and do not install or upgrade global
packages.

Check whether Docker and Docker Compose are available. If the repository does not
already have a root .env file, copy .env.example to .env. Do not print, log, commit,
or reveal any secret values. Pause after preparing the file and tell me which four
required variable names I need to fill in.

After I tell you the .env file is ready, validate the configuration without showing
its contents. Build and start the bot, check its health, and inspect only bounded
recent logs. Explain any problem in plain English. If startup succeeds, tell me how
to perform one private Discord test.
```

The agent should create `.env` as a **file** in the root of this repository. Paste
your four values into it:

```dotenv
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_CHANNEL_IDS=123456789012345678
ASSEMBLYAI_API_KEY=...
OPENAI_API_KEY=...
```

If you have several allowed channels, separate their IDs with commas. Keep `.env`
private and never commit it. When the file is ready, tell the agent:

```text
The root .env file is ready. Continue the setup and verification.
```

Setup is complete when the `bot` service is healthy and its logs contain
`Discord bot is connected and ready`.

To test it, post a YouTube link in one of the allowed channels. The bot should reply
with a summary and a `.txt` transcript.

## What you can send

| Input | What to post in an allowed Discord channel |
| --- | --- |
| YouTube video | A `youtube.com` or `youtu.be` link |
| Spotify episode | The episode link; Spotify credentials are required |
| Apple Podcasts | An episode link, or a show link to use its latest episode |
| RSS feed | `!podcast https://example.com/feed.xml` |
| Publisher episode page | `!podcast URL`; the page must identify a matching RSS episode |
| Podcast search | `!podcast Show name \| Exact episode title` |
| Audio or video file | Attach the file directly to the message |

YouTube, Spotify, and Apple Podcasts links are recognized automatically. Other URLs
must begin with `!podcast`; this prevents the bot from fetching an ordinary link by
mistake. If a message contains several supported items, the bot processes only the
first one.

## Optional features

Spotify episode links require both `SPOTIFY_CLIENT_ID` and
`SPOTIFY_CLIENT_SECRET`. PodcastIndex search requires both
`PODCAST_INDEX_API_KEY` and `PODCAST_INDEX_API_SECRET`. Apple Podcasts and direct
RSS feeds do not require extra credentials.

If YouTube blocks requests from your server, you can configure a proxy with the
`YOUTUBE_PROXY_*` settings in `.env.example`. Proxy credentials belong in the
username and password fields, not in the host. Direct fallback is disabled unless
you explicitly set `YOUTUBE_ALLOW_DIRECT_FALLBACK=true`, so a failed proxy does not
silently expose the server's IP address.

For age-restricted or bot-protected YouTube videos, you can mount a private
Netscape-format cookie file into the container. Keep the file outside the repository
and add this Compose override:

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

Then add `YOUTUBE_COOKIES_FILE=/run/secrets/youtube-cookies.txt` to `.env`.

## Limits and privacy

By default, uploads are limited to 500 MiB, remote podcast media to 512 MiB, media
length to six hours, and transcript text to 1.5 million characters. The YouTube
length check happens when the bot needs to download audio; videos handled from
existing captions do not go through that download check. Podcast media must provide
a duration and a non-zero size through its RSS feed or HTTP response.

The bot rejects unsupported media, unsafe redirects, private-network podcast URLs,
oversized responses, bots, and webhooks. Docker runs it as a non-root user with a
read-only filesystem and limited resources.

Discord receives the original request and the reply. AssemblyAI receives media when
transcription is needed. OpenAI receives the transcript and a small amount of source
context to create the summary; API response storage is disabled. YouTube, podcast
providers, and any proxy you configure receive only the requests needed to retrieve
the media. Only process media you are allowed to use.

Secrets, cookies, databases, logs, and caches are excluded from Git and the Docker
build context. See [SECURITY.md](SECURITY.md) for more detail.

## Development

For local development without Docker, use Python 3.11 or 3.12 and install ffmpeg,
ffprobe, Node.js, and yt-dlp:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
pytest
ruff check .
mypy
```

The automated tests are offline and use fake credentials. Run the first live test
in a private Discord channel.

## License

MIT. See [LICENSE](LICENSE).
