#!/usr/bin/env python3
"""Bootstrap + run for Pterodactyl-style hosts (Wispbyte) — no root, no Docker.

Most Python eggs hardcode their startup command to `python <file>`, so the
bootstrap has to *be* Python rather than a shell script. Point the egg's script
variable at `wispbyte/bootstrap.py`.

On first boot this downloads static ffmpeg / cloudflared / deno into ./bin and
puts that on PATH, installs Python deps when requirements.txt changes, starts
the tunnel in the background, then execs main.py in place so the panel's Stop
button signals the bot directly.
"""

import hashlib
import io
import os
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT / "bin"
CACHE_DIR = ROOT / ".bootstrap"

FF_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
          "ffmpeg-master-latest-linux64-gpl.tar.xz")
CF_URL = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
          "cloudflared-linux-amd64")
DENO_URL = ("https://github.com/denoland/deno/releases/latest/download/"
            "deno-x86_64-unknown-linux-gnu.zip")


def log(msg):
    print("[bootstrap] %s" % msg, flush=True)


def fetch(url, pairs):
    """Download `url` once and write out each (dest, member) from the archive.

    A member of None means the URL is the bare binary. Uses only the stdlib —
    curl, unzip and xz are not guaranteed to exist in the egg's image.
    """
    log("downloading %s ..." % ", ".join(d.name for d, _ in pairs))
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req) as r:
        blob = r.read()

    def extract(member):
        if member is None:
            return blob
        if url.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                return z.read(next(n for n in z.namelist() if n.endswith(member)))
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:xz") as t:
            m = next(m for m in t.getmembers() if m.name.endswith(member))
            return t.extractfile(m).read()

    for dest, member in pairs:
        # Write to a temp name first so an interrupted boot can't leave a
        # truncated-but-executable binary that we'd then skip re-downloading.
        tmp = dest.with_suffix(".partial")
        with open(tmp, "wb") as f:
            f.write(extract(member))
        os.chmod(tmp, os.stat(tmp).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        tmp.replace(dest)
        log("installed %s" % dest)


def ensure_binaries():
    ffmpeg, ffprobe = BIN_DIR / "ffmpeg", BIN_DIR / "ffprobe"
    if not (os.access(ffmpeg, os.X_OK) and os.access(ffprobe, os.X_OK)):
        fetch(FF_URL, [(ffmpeg, "bin/ffmpeg"), (ffprobe, "bin/ffprobe")])

    cloudflared = BIN_DIR / "cloudflared"
    if os.getenv("CLOUDFLARE_TUNNEL_TOKEN") and not os.access(cloudflared, os.X_OK):
        fetch(CF_URL, [(cloudflared, None)])

    # yt-dlp's JS runtime for YouTube signature solving. Optional: without it
    # some formats go missing. Only runs during extraction.
    deno = BIN_DIR / "deno"
    if os.getenv("ENABLE_DENO", "1") == "1" and not os.access(deno, os.X_OK):
        fetch(DENO_URL, [(deno, "deno")])


_PIP_VARIANTS = [
    ["--user"],
    ["--user", "--break-system-packages"],
    [],
    ["--break-system-packages"],
]

_pip_flags = None


def pip_install(args):
    global _pip_flags
    base = [sys.executable, "-m", "pip", "install", "--no-cache-dir"]

    if _pip_flags is not None:
        return subprocess.call(base + _pip_flags + args) == 0

    for flags in _PIP_VARIANTS:
        if subprocess.call(base + flags + args) == 0:
            _pip_flags = flags
            log("pip flags: %s" % (" ".join(flags) or "(none)"))
            return True
        log("pip install with %s failed, trying next strategy"
            % (" ".join(flags) or "(no flags)"))
    return False


def ensure_deps():
    req = ROOT / "requirements.txt"
    marker = CACHE_DIR / "req.md5"
    digest = hashlib.md5(req.read_bytes()).hexdigest()

    # pip is the single biggest memory spike on a 1GB box — don't run it on
    # every boot, only when requirements.txt actually changed.
    if marker.exists() and marker.read_text().strip() == digest:
        log("dependencies up to date")
    else:
        log("installing Python dependencies ...")
        if not pip_install(["-r", str(req)]):
            log("dependency install FAILED — the bot will probably not start")
            sys.exit(1)
        marker.write_text(digest)

    # yt-dlp goes stale fast; refresh it on every boot (pure-python wheel).
    if os.getenv("AUTO_UPDATE_YTDLP", "1") == "1":
        if not pip_install(["--upgrade", "yt-dlp"]):
            log("yt-dlp update failed, continuing with installed version")


def load_env_file():
    """Read .env into os.environ before anything else needs it.

    main.py calls load_dotenv(), but that happens after this script has already
    decided whether to fetch cloudflared and start the tunnel — so without this
    a CLOUDFLARE_TUNNEL_TOKEN sitting in .env would be invisible and the tunnel
    would silently never come up. Hand-rolled because python-dotenv is one of
    the dependencies we may not have installed yet.

    Real environment variables win, matching load_dotenv()'s default.
    """
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def ensure_activity_secret():
    """Generate a persistent ACTIVITY_SECRET on first boot.

    Without one, session.py falls back to DISCORD_CLIENT_SECRET, and failing
    that to a per-process random key — which silently invalidates every
    Activity session on restart. Generating a dedicated key here also avoids
    reusing the OAuth client secret as a token-signing key.
    """
    if os.getenv("ACTIVITY_SECRET"):
        return

    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("ACTIVITY_SECRET="):
                return  # main.py's load_dotenv() will pick it up

    import secrets
    value = secrets.token_hex(32)
    with open(env_file, "a", encoding="utf-8") as f:
        f.write("\n# Generated by wispbyte/bootstrap.py on first boot\n")
        f.write("ACTIVITY_SECRET=%s\n" % value)
    os.environ["ACTIVITY_SECRET"] = value
    log("generated ACTIVITY_SECRET and saved it to .env")


def check_frontend():
    if not (ROOT / "activity-frontend" / "out" / "index.html").exists():
        log("WARNING: activity-frontend/out is missing — the bot will run but "
            "the Discord Activity UI will 404. Build it locally and upload it; "
            "it cannot be built here (no Node, and the build needs >1GB RAM).")


def main():
    os.chdir(ROOT)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    os.environ["PATH"] = "%s%s%s" % (BIN_DIR, os.pathsep, os.environ.get("PATH", ""))
    os.environ["PYTHONUNBUFFERED"] = "1"

    load_env_file()
    ensure_activity_secret()
    check_frontend()
    ensure_binaries()
    ensure_deps()

    # Bind FastAPI to the port the panel allocated.
    port = os.getenv("SERVER_PORT") or os.getenv("ACTIVITY_PORT") or "8080"
    os.environ["ACTIVITY_PORT"] = port

    try:
        banner = subprocess.check_output([str(BIN_DIR / "ffmpeg"), "-version"],
                                         stderr=subprocess.STDOUT)
        log(banner.decode("utf-8", "replace").splitlines()[0])
    except Exception as e:
        log("ffmpeg check failed: %s" % e)

    if os.getenv("CLOUDFLARE_TUNNEL_TOKEN"):
        log("starting cloudflared -> localhost:%s" % port)
        subprocess.Popen([
            str(BIN_DIR / "cloudflared"), "tunnel", "--no-autoupdate",
            "run", "--token", os.environ["CLOUDFLARE_TUNNEL_TOKEN"],
        ])

    log("starting bot on port %s" % port)
    # exec, not Popen: the bot takes over this PID so the panel's Stop button
    # delivers SIGTERM to it rather than to a supervising wrapper.
    os.execv(sys.executable, [sys.executable, str(ROOT / "main.py")])


if __name__ == "__main__":
    main()
