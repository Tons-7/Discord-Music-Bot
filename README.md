# Discord Music Bot

A **modular Discord music bot** written in Python using `discord.py`.
Supports YouTube, Spotify, and SoundCloud playback with queue management, playlists, audio effects, lyrics,
autoplay, and an optional **Discord Activity** (in-Discord web UI) for browser-based playback.

## Features

- Play music from YouTube, Spotify, SoundCloud, or by search query
- Queue, history, and playlist management (server and global playlists)
- Playback controls: skip, pause, resume, seek, loop, shuffle
- Audio effects: bass boost, nightcore, vaporwave, treble boost, 8D audio
- Speed control with pitch preservation
- Autoplay mode using Last.fm recommendations
- Lyrics via lrclib.net (including synced LRC)
- Audio file caching for near-instant seeking
- Leaderboard and listening statistics
- DJ role support for permission control
- Auto-reconnect on voice disconnection
- Queue persistence across bot restarts
- **Discord Activity**: browser-based player that runs inside Discord with search,
  queue, playlists, favorites, lyrics, history, and a picture-in-picture mode

## Requirements

- Python 3.11+
- [FFmpeg](https://www.ffmpeg.org/download.html) installed and on PATH
- Node.js 20+ (only if you want to build the Discord Activity frontend)

## Setup

1. Clone the repository
2. Install Python dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your tokens:
   ```
   cp .env.example .env
   ```
4. (Optional) Build the Discord Activity frontend:
   ```
   cd activity-frontend
   cp .env.local.example .env.local   # fill in NEXT_PUBLIC_DISCORD_CLIENT_ID
   npm install
   npm run build
   cd ..
   ```
5. Run the bot:
   ```
   python main.py
   ```

## Docker

```
docker compose up --build
```

The Docker image builds the Activity frontend during the image build, so Node.js is not required on the host.

## Configuration

All configuration is done through environment variables in `.env`:

| Variable                | Required                 | Description                                    |
|-------------------------|--------------------------|------------------------------------------------|
| `BOT_TOKEN`             | Yes                      | Discord bot token                              |
| `SPOTIFY_CLIENT_ID`     | No                       | Spotify API client ID                          |
| `SPOTIFY_CLIENT_SECRET` | No                       | Spotify API client secret                      |
| `LASTFM_API_KEY`        | No                       | Last.fm API key (for autoplay)                 |
| `LASTFM_API_SECRET`     | No                       | Last.fm API secret                             |
| `DISCORD_CLIENT_ID`     | Activity only            | Discord application client ID                  |
| `DISCORD_CLIENT_SECRET` | Activity only            | Discord application client secret              |
| `ACTIVITY_PORT`         | No (default `8080`)      | Port the FastAPI/Activity server listens on    |

Spotify credentials can be obtained from the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
Last.fm credentials can be obtained from [Last.fm API](https://www.last.fm/api/account/create).

## Discord Activity

The bot ships with an optional Activity that lets users play music in a browser pane inside Discord
(no voice-channel connection required).

To enable it:

1. In the [Discord Developer Portal](https://discord.com/developers/applications), open your application and
   enable **Activities**.
2. Configure **URL Mappings** to route the Activity to the host running this bot on `ACTIVITY_PORT`.
3. Set `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET` in `.env`.
4. Build the frontend (step 4 of Setup above) and run `python main.py`. The Activity is served by the same process.

Architecture notes live in `activity-frontend/README.md` and in `CLAUDE.md`.

Make sure `.env` remains in `.gitignore` so it is **never committed**.

## Disclaimer

This bot is only for personal use.
