#!/usr/bin/env bash
# Offline checks of a built image (never starts the bot): docker_smoke_test.sh IMAGE
set -euo pipefail

image=${1:?usage: docker_smoke_test.sh IMAGE}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
run() { docker run --rm --network none "$@"; }

echo "--- runs as uid 10001 with the tools on PATH"
test "$(run --entrypoint id "$image" -u)" = 10001
run --entrypoint ffmpeg "$image" -hide_banner -version | sed -n 1p
run --entrypoint deno "$image" --version | sed -n 1p

echo "--- deno solves without writing to stderr (yt-dlp treats any stderr output as a failure)"
err=$(echo 'console.log(1 + 1)' | run -i --entrypoint deno "$image" run --ext=js --no-code-cache --no-prompt \
  --no-remote --no-lock --node-modules-dir=none --no-config --no-npm --cached-only - 2>&1 >/dev/null)
test -z "$err" || { echo "deno stderr: $err"; exit 1; }

echo "--- the app imports"
run --entrypoint python "$image" -c "import activity.app, main"

echo "--- healthcheck fails while nothing listens and passes once /api/config answers"
if run --entrypoint python "$image" /app/scripts/docker_healthcheck.py; then
  echo "healthcheck passed with nothing listening"; exit 1
fi
run --entrypoint sh "$image" -c 'mkdir -p /tmp/stub/api && echo "{}" > /tmp/stub/api/config &&
  (python -m http.server 8080 --bind 127.0.0.1 --directory /tmp/stub >/dev/null 2>&1 &) && sleep 2 &&
  python /app/scripts/docker_healthcheck.py'

echo "--- the entrypoint imports a legacy music_bot.db and audio cache into /data"
mkdir -p "$work/legacy/audio_cache/activity"
python3 -c "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL');
c.execute('CREATE TABLE t (v TEXT)'); c.execute(\"INSERT INTO t VALUES ('kept')\"); c.commit(); c.close()" \
  "$work/legacy/music_bot.db"
echo song > "$work/legacy/audio_cache/abc.opus"
echo partial > "$work/legacy/audio_cache/abc.webm.part"
echo song > "$work/legacy/audio_cache/activity/def.m4a"
chmod -R a+rX "$work/legacy"
run -v "$work/legacy:/legacy:ro" "$image" python -c "
import os, sqlite3
assert sqlite3.connect(os.environ['DB_PATH']).execute('SELECT v FROM t').fetchone() == ('kept',)
assert sorted(os.listdir('audio_cache')) == ['abc.opus', 'activity'], os.listdir('audio_cache')
assert os.listdir('audio_cache/activity') == ['def.m4a']
assert os.path.exists('.legacy-imported')
"

echo "--- an unreadable legacy database stops the container instead of starting empty"
chmod 000 "$work/legacy/music_bot.db"
if run -v "$work/legacy:/legacy:ro" "$image" true; then
  echo "container started although the legacy database could not be read"; exit 1
fi

echo "smoke test passed"
