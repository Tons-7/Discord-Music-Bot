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

From the server console:

```bash
git clone https://github.com/Tons-7/Discord-Music-Bot.git .
```

`activity-frontend/out/` is committed to the repo specifically so this works — a Python
egg has no Node, and the Next build peaks well over 1 GB anyway, so it can't be built
on the host. **After any UI change you must rebuild and commit it**, or the host keeps
serving the old bundle:

```bash
cd activity-frontend && npm run build
git add activity-frontend/out && git commit -m "rebuild frontend"
```

If `out/` is ever missing the bot still runs fine — it logs a warning and serves no
Activity UI.

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

`.env` is gitignored, so create `/home/container/.env` yourself — easiest in the
panel's **Files** page, which avoids putting secrets through SFTP or git. Use the keys
from `.env.example`: `BOT_TOKEN`, optionally Spotify/Last.fm, plus `DISCORD_CLIENT_ID`
/ `DISCORD_CLIENT_SECRET` and `CLOUDFLARE_TUNNEL_TOKEN` for the Activity and tunnel.

`ACTIVITY_SECRET` is generated for you on first boot and appended to `.env`, so you
don't need to set it. It signs Activity session tokens; without a stable value, every
restart invalidates outstanding sessions. (`session.py` would otherwise fall back to
`DISCORD_CLIENT_SECRET`, which works but reuses an OAuth secret as a signing key.)

The bootstrap parses `.env` itself before `main.py` gets a chance to — otherwise
`CLOUDFLARE_TUNNEL_TOKEN` would be invisible at the point where it decides whether to
start the tunnel, and the tunnel would silently never come up. Real environment
variables set in the panel take precedence over `.env`, matching `load_dotenv()`.

Do **not** set `ACTIVITY_PORT` — the bootstrap binds it to `$SERVER_PORT`, the port the
panel allocated. Your cloudflared tunnel's public hostname should point at
`http://localhost:$SERVER_PORT`, so check the allocation in the panel and update the
tunnel's ingress rule in the Cloudflare Zero Trust dashboard to match.

Optional knobs (read from the environment or `.env`):

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

The panel runs one command, so `bootstrap.py` backgrounds cloudflared and then `exec`s
Python. `exec` matters: the bot takes over the bootstrap's own PID, so the panel's Stop
button delivers SIGTERM to the bot rather than to a supervising wrapper that would have
to forward it. cloudflared dies with the container.
