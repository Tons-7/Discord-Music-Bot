#!/bin/bash
set -eu

cd "$(dirname "$0")/.."

PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then
    echo "[bootstrap] no python on PATH — is this a Python egg?" >&2
    exit 1
fi

exec "$PY" wispbyte/bootstrap.py
