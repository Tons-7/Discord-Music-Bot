#!/usr/bin/env python3
"""Supervise the music bot and the game tracker in one Pterodactyl container.

The panel runs a single command, so this process owns both bots: it starts each
in its own directory, forwards the panel's Stop signal to both, and restarts a
child that dies on its own. Point the egg's PY_FILE at this file.

Expected layout (override either path with MUSIC_BOT_DIR / GAME_TRACKER_DIR):

    /home/container/
      music-bot/       <- this repo, with its own .env
      game-tracker/    <- Game-Tracker-Bot, with its own .env

Do NOT put a .env at /home/container itself: the game tracker calls a bare
load_dotenv(), which walks up the tree, and it would find that file and try to
log in with the wrong token.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

MUSIC_DIR = Path(os.getenv("MUSIC_BOT_DIR") or Path(__file__).resolve().parents[1])
GAMES_DIR = Path(os.getenv("GAME_TRACKER_DIR") or MUSIC_DIR.parent / "game-tracker")

# name -> (working directory, argv). The music bot goes through bootstrap.py so
# it still gets ffmpeg, cloudflared and the tunnel; bootstrap execs main.py in
# place, so the PID we hold stays the right one to signal.
BOTS = {
    "music": (MUSIC_DIR, [sys.executable, str(MUSIC_DIR / "wispbyte" / "bootstrap.py")]),
    "games": (GAMES_DIR, [sys.executable, "main.py"]),
}

# If a child dies instantly and repeatedly, stop rather than spin in a restart
# loop that buries the actual error under thousands of lines.
MAX_RESTARTS = 5
RESTART_WINDOW = 120
RESTART_DELAY = 5

_procs = {}
_restarts = {}
_stopping = False


def log(msg):
    print("[run] %s" % msg, flush=True)


def git_pull(name):
    """Update a checkout before starting it.

    The egg's own AUTO_UPDATE only pulls /home/container, which stops being a
    checkout once both bots live in subfolders — so without this there is no
    way to deploy an update except by hand. Set AUTO_UPDATE=0 to skip.

    --ff-only on purpose: if a checkout has diverged or has local edits, say so
    and start the old code rather than silently building a merge commit on a
    server nobody is watching.
    """
    cwd = BOTS[name][0]
    if not (cwd / ".git").is_dir():
        log("%s: not a git checkout, skipping pull" % name)
        return
    try:
        r = subprocess.run(
            ["git", "pull", "--ff-only"], cwd=str(cwd),
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        log("%s: git pull failed (%s) — starting existing code" % (name, e))
        return

    # Prefer stdout ("Already up to date." / "Fast-forward") over stderr, which
    # ends on a ref-range line that says nothing about what actually happened.
    def last_line(text):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return lines[-1] if lines else ""

    if r.returncode == 0:
        log("%s: %s" % (name, last_line(r.stdout) or last_line(r.stderr) or "pulled"))
    else:
        log("%s: git pull FAILED — %s — starting existing code"
            % (name, last_line(r.stderr) or last_line(r.stdout) or r.returncode))


def spawn(name):
    cwd, argv = BOTS[name]
    if not cwd.is_dir():
        log("MISSING: %s does not exist, so %s cannot start" % (cwd, name))
        return None
    if not (cwd / ".env").exists():
        # Not fatal — the panel may supply real env vars instead — but it is
        # the most likely reason a bot silently fails to log in.
        log("WARNING: no .env in %s; %s will rely on panel env vars" % (cwd, name))
    log("starting %s in %s" % (name, cwd))
    return subprocess.Popen(argv, cwd=str(cwd))


def shutdown(signum, _frame):
    global _stopping
    if _stopping:
        return
    _stopping = True
    log("got signal %s — stopping both bots" % signum)
    for name, proc in _procs.items():
        if proc and proc.poll() is None:
            log("terminating %s (pid %d)" % (name, proc.pid))
            proc.terminate()


def main():
    # Children inherit this, so their logs are not held in a pipe buffer.
    os.environ["PYTHONUNBUFFERED"] = "1"

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Pull once at boot, not on every restart — a crash-looping bot should not
    # hammer the remote, and the code it crashed on is the code to debug.
    if os.getenv("AUTO_UPDATE", "1") != "0":
        for name in BOTS:
            if BOTS[name][0].is_dir():
                git_pull(name)

    for name in BOTS:
        _procs[name] = spawn(name)
        _restarts[name] = []

    if not any(_procs.values()):
        log("no bots could be started — check the directory layout")
        return 1

    while not _stopping:
        time.sleep(2)
        for name, proc in list(_procs.items()):
            if proc is None or proc.poll() is None:
                continue

            log("%s exited with code %s" % (name, proc.returncode))

            now = time.monotonic()
            recent = [t for t in _restarts[name] if now - t < RESTART_WINDOW]
            if len(recent) >= MAX_RESTARTS:
                log("%s has died %d times in %ds — giving up so the panel can "
                    "restart cleanly. Scroll up for its real error."
                    % (name, len(recent), RESTART_WINDOW))
                shutdown("self", None)
                break

            recent.append(now)
            _restarts[name] = recent
            time.sleep(RESTART_DELAY)
            if _stopping:
                break
            _procs[name] = spawn(name)

    # Give both a moment to exit on their own before forcing it.
    deadline = time.monotonic() + 15
    for name, proc in _procs.items():
        if not proc:
            continue
        remaining = max(0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            log("%s did not stop in time — killing it" % name)
            proc.kill()

    log("both bots stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
