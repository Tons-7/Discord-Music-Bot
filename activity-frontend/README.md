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
npm install
npm run build        # writes out/, which `uv run python main.py` serves at ACTIVITY_PORT
npm run typecheck    # tsgo --noEmit
npm test             # vitest unit tests for src/lib
```

The Activity calls its API on the same origin, so it only works when served by the bot's FastAPI app (open it in
Discord through the URL mapping). After a change, rebuild and reload the Activity; the backend serves `out/` from disk.
`out/` is committed: Wispbyte has no Node, so commit the rebuilt export with every UI change.

## Setup inside Discord

See the [Discord Activity section of the root README](../README.md#discord-activity) for Developer Portal,
URL Mapping, and client-credential setup.
