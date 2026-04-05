# Discord Music Bot

A **modular Discord music bot** written in Python using `discord.py`.  
Supports YouTube, Spotify, and SoundCloud playback with queue management, playlists, audio effects, lyrics, and
autoplay.

## Features

- Play music from YouTube, Spotify, SoundCloud, or by search query
- Queue, history, and playlist management (server and global playlists)
- Playback controls: skip, pause, resume, seek, loop, shuffle
- Audio effects: bass boost, nightcore, vaporwave, treble boost, 8D audio
- Speed control with pitch preservation
- Autoplay mode using Last.fm recommendations
- Lyrics via lrclib.net
- Audio file caching for near-instant seeking
- Leaderboard and listening statistics
- DJ role support for permission control
- Auto-reconnect on voice disconnection
- Queue persistence across bot restarts

## Requirements

- Python 3.11+
- [FFmpeg](https://www.ffmpeg.org/download.html) installed and on PATH

## Setup

1. Clone the repository
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your tokens:
   ```
   cp .env.example .env
   ```
4. Run the bot:
   ```
   python main.py
   ```

## Docker

```
docker compose up --build
```

## Configuration

All configuration is done through environment variables in `.env`:

| Variable                | Required | Description                    |
|-------------------------|----------|--------------------------------|
| `BOT_TOKEN`             | Yes      | Discord bot token              |
| `SPOTIFY_CLIENT_ID`     | No       | Spotify API client ID          |
| `SPOTIFY_CLIENT_SECRET` | No       | Spotify API client secret      |
| `LASTFM_API_KEY`        | No       | Last.fm API key (for autoplay) |
| `LASTFM_API_SECRET`     | No       | Last.fm API secret             |

Spotify credentials can be obtained from the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).  
Last.fm credentials can be obtained from [Last.fm API](https://www.last.fm/api/account/create).

Make sure `.env` remains in `.gitignore` so it is **never committed**.

## Disclaimer

This bot is only for personal use.
