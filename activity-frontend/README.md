# Discord Music Bot — Activity Frontend

Browser-based UI for the [Discord Music Bot](../README.md), designed to run as a
[Discord Activity](https://discord.com/developers/docs/activities/overview) inside a voice or text channel.

Built with Next.js (static export), Tailwind CSS, and the
[Discord Embedded App SDK](https://discord.com/developers/docs/developer-tools/embedded-app-sdk).
Static output is served by the FastAPI backend that runs in the same process as the bot.

## Features

- Now Playing, queue, search, playlists (server + global), favorites, lyrics, history, and stats
- Audio plays directly in the browser via an HTML `<audio>` element — no voice channel required
- Picture-in-picture layout when Discord minimizes the Activity
- Real-time state sync with the bot over WebSocket

## Local development

```bash
cp .env.local.example .env.local   # fill in NEXT_PUBLIC_DISCORD_CLIENT_ID
npm install
npm run dev
```

The dev server only renders the UI — it talks to the FastAPI backend from the bot process, so run
`python main.py` from the repo root in parallel.

## Production build

```bash
npm run build
```

Emits a static export into `out/`, which the FastAPI app serves from `ACTIVITY_PORT`.

## Setup inside Discord

See the [Discord Activity section of the root README](../README.md#discord-activity) for Developer Portal,
URL Mapping, and client-credential setup.
