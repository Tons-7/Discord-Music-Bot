#!/bin/bash
# Bootstrap + run for Pterodactyl-style hosts (Wispbyte) — no root, no Docker.
#
# Downloads static ffmpeg / cloudflared / deno into ./bin on first boot, installs
# Python deps when requirements.txt changes, starts the tunnel in the background,
# then execs the bot so the panel's Stop button reaches it.
set -euo pipefail

cd /home/container

BIN_DIR="$PWD/bin"
CACHE_DIR="$PWD/.bootstrap"
export PATH="$BIN_DIR:$PATH"
export PYTHONUNBUFFERED=1

mkdir -p "$BIN_DIR" "$CACHE_DIR"

PY=$(command -v python3 || command -v python)

# fetch <url> <dest> <member> [<dest> <member> ...] — pull one or more binaries
# out of a remote archive in a single download. Uses only the stdlib: curl, unzip
# and xz are not guaranteed to exist in the egg's image, but Python is.
# A member of "-" means the URL is the bare binary.
fetch() {
    "$PY" - "$@" <<'PYEOF'
import io, os, stat, sys, tarfile, urllib.request, zipfile

url, pairs = sys.argv[1], list(zip(sys.argv[2::2], sys.argv[3::2]))
print(f"[bootstrap] downloading {', '.join(os.path.basename(d) for d, _ in pairs)} ...", flush=True)
req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
with urllib.request.urlopen(req) as r:
    blob = r.read()

def extract(member):
    if member == "-":
        return blob
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            return z.read(next(n for n in z.namelist() if n.endswith(member)))
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:xz") as t:
        return t.extractfile(next(m for m in t.getmembers() if m.name.endswith(member))).read()

for dest, member in pairs:
    with open(dest, "wb") as f:
        f.write(extract(member))
    os.chmod(dest, os.stat(dest).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    print(f"[bootstrap] installed {dest}", flush=True)
PYEOF
}

FF_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"

if [ ! -x "$BIN_DIR/ffmpeg" ] || [ ! -x "$BIN_DIR/ffprobe" ]; then
    fetch "$FF_URL" "$BIN_DIR/ffmpeg" "/bin/ffmpeg" "$BIN_DIR/ffprobe" "/bin/ffprobe"
fi

if [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ] && [ ! -x "$BIN_DIR/cloudflared" ]; then
    fetch "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" \
        "$BIN_DIR/cloudflared" "-"
fi

# yt-dlp's JS runtime for YouTube signature solving. Optional: without it some
# formats go missing. ~110MB on disk, and it only runs during extraction.
if [ "${ENABLE_DENO:-1}" = "1" ] && [ ! -x "$BIN_DIR/deno" ]; then
    fetch "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip" \
        "$BIN_DIR/deno" "deno"
fi

# Reinstall deps only when requirements.txt actually changes — pip is the
# single biggest memory spike on a 1GB box, so don't run it every boot.
REQ_HASH=$("$PY" -c "import hashlib,sys;print(hashlib.md5(open('requirements.txt','rb').read()).hexdigest())")
if [ "$(cat "$CACHE_DIR/req.md5" 2>/dev/null || true)" != "$REQ_HASH" ]; then
    echo "[bootstrap] installing Python dependencies ..."
    "$PY" -m pip install --no-cache-dir --upgrade pip
    "$PY" -m pip install --no-cache-dir -r requirements.txt
    echo "$REQ_HASH" > "$CACHE_DIR/req.md5"
else
    echo "[bootstrap] dependencies up to date"
fi

# yt-dlp goes stale fast; refresh it on every boot (cheap, pure-python wheel).
if [ "${AUTO_UPDATE_YTDLP:-1}" = "1" ]; then
    "$PY" -m pip install --no-cache-dir --upgrade yt-dlp || \
        echo "[bootstrap] yt-dlp update failed, continuing with installed version"
fi

# Bind FastAPI to the port the panel allocated.
export ACTIVITY_PORT="${SERVER_PORT:-${ACTIVITY_PORT:-8080}}"

ffmpeg -version | head -n 1

if [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
    echo "[bootstrap] starting cloudflared -> localhost:$ACTIVITY_PORT"
    "$BIN_DIR/cloudflared" tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN" &
fi

echo "[bootstrap] starting bot on port $ACTIVITY_PORT"
exec "$PY" main.py
