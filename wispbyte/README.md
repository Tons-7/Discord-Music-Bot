# Hosting on Wispbyte (Pterodactyl, no Docker)

Wispbyte gives you one unprivileged container with a writable `/home/container`, a
single startup command, SFTP, and a console. You can't run `docker compose`, and you
can't `apt install ffmpeg` — so `bootstrap.py` fetches static builds of ffmpeg,
cloudflared and deno into `./bin` on first boot and puts that directory on `PATH`.

Everything else — voice, the FastAPI Activity server, the tunnel — runs unchanged.

## 1. Create the server

Pick a **Python** egg (generic Python / Python bot). Choose the **Python 3.13** image
if you get a choice — `davey` (discord.py's voice encryption lib) publishes manylinux
wheels only through cp313, so on 3.14 pip would try to compile it from source and fail.

## 2. Upload the code

Via SFTP into `/home/container`, or from the console:

```bash
git clone https://github.com/Tons-7/Discord-Music-Bot.git .
```

The Next.js build (`activity-frontend/out/`) is gitignored, so it won't come along.
**Build it locally and upload `out/` by SFTP** to `activity-frontend/out` — it's ~1.2 MB:

```bash
cd activity-frontend && npm run build
```

Don't try to `npm run build` on the host: the Next build peaks well over 1 GB and
there's no Node in a Python egg anyway. If `out/` is missing the bot still runs fine,
it just logs a warning and serves no Activity UI.

## 3. Startup command

Python eggs almost always hardcode their startup to `python <file>` and only let you
choose the file. So point that variable — usually labelled "Python File", "App
File", `PY_FILE` or similar — at:

```
wispbyte/bootstrap.py
```

Do **not** point it at `main.py`: the bootstrap has to run first to install ffmpeg and
the dependencies, and it `exec`s `main.py` itself when it's done.

If your egg does let you set a full shell command, `bash wispbyte/start.sh` works too —
it's a thin wrapper that just calls `bootstrap.py`, so there's one implementation.

## 4. Environment

Create `/home/container/.env` (SFTP or the file manager) with the usual keys from
`.env.example` — `BOT_TOKEN`, optionally Spotify/Last.fm, and for the Activity:

```
DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=
ACTIVITY_SECRET=<random hex; without it sessions die on every restart>
CLOUDFLARE_TUNNEL_TOKEN=
```

Do **not** set `ACTIVITY_PORT` — `start.sh` binds it to `$SERVER_PORT`, the port the
panel allocated. Your cloudflared tunnel's public hostname should point at
`http://localhost:$SERVER_PORT`, so check the allocation in the panel and update the
tunnel's ingress rule in the Cloudflare Zero Trust dashboard to match.

Optional knobs read by `start.sh`:

| Var | Default | Effect |
|---|---|---|
| `ENABLE_DENO` | `1` | Set `0` to skip deno (~110 MB, saves RAM during extraction) |
| `AUTO_UPDATE_YTDLP` | `1` | Set `0` to stop upgrading yt-dlp on every boot |

## 5. First boot

Expect several minutes. First boot downloads ~170 MB (the ffmpeg tarball alone is
127 MB). Measured on a test run: `bin/` lands at 279 MB with ffmpeg + ffprobe, ~390 MB
once deno is added — BtbN's build is a full GPL monolith, so the two binaries are
~146 MB each. Irrelevant against 10 GB, but it's why the first start is slow. Then
`pip install` runs.

`pip` flags vary by image — unprivileged containers can't write to a root-owned
site-packages, and Debian-based images refuse `--user` under PEP 668. The bootstrap
tries `--user`, then `--break-system-packages`, then a plain install, and logs which
one worked (`[bootstrap] pip flags: ...`).

Subsequent boots skip both (deps reinstall only when `requirements.txt`'s hash
changes). Watch for `ffmpeg version ...` then `[bootstrap] starting bot on port ...`.

Binary size doesn't cost RAM — ELF pages are demand-loaded, so a transcoding ffmpeg
is ~40 MB resident regardless.

## Living within 1 GB of RAM

Rough steady state: Python + discord.py + FastAPI ≈ 250–350 MB, plus ~40 MB per active
FFmpeg voice stream. yt-dlp extraction is the spike — it's what will OOM you, and
Pterodactyl kills the container rather than swapping.

If you hit OOM kills:

- Drop `bot.executor` from 3 workers to 2 in `bot.py` — three concurrent yt-dlp
  extractions is the worst case on this box.
- Set `ENABLE_DENO=0`. You lose some YouTube formats but save the interpreter spawn.
- Lower `MAX_CACHE_SIZE` in `config.py` (500 song-metadata entries is generous).
- Serve few guilds. This is a one-guild-at-a-time machine, realistically.

Disk is not a concern at 10 GB, but note `audio_cache/` has **no eviction** — the
Activity M4A cache grows forever. Mine is already 1.2 GB locally. Either clear it
periodically from the console (`rm -rf audio_cache/activity/*`) or add a size cap.

## Why cloudflared runs in-process

The panel runs one command, so `start.sh` backgrounds cloudflared and then `exec`s
Python. `exec` matters: it makes the bot PID 1's direct child so the panel's Stop
button delivers SIGTERM to the bot, not to a wrapper shell. cloudflared dies with the
container.
