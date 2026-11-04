# Images are pinned by tag and digest; Dependabot's docker ecosystem bumps both together.

### Stage 1: Build Next.js frontend
FROM node:26.12.1-slim@sha256:94816674c62774da0d099a947bf4613b9e1972a2733abd0d0cd80e795f8411d0 AS frontend
WORKDIR /frontend
COPY activity-frontend/package.json activity-frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY activity-frontend/ .
RUN npm run build

# JS runtime for yt-dlp's YouTube signature solving
FROM denoland/deno:bin-2.9.8@sha256:b507583d3c68ba69f3ed2ce5969ae5b8b70f43421c781279c7aea06435263543 AS deno

# Keep in step with the uv version pinned in .github/workflows/ci.yml.
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

### Stage 2: Python dependencies (pyproject.toml + uv.lock)
FROM python:3.14.9-slim-trixie@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8 AS builder
COPY --from=uv /uv /usr/local/bin/uv
# Never fetch a standalone interpreter; use this image's python3.14.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project

### Stage 3: Runtime
FROM python:3.14.9-slim-trixie@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno

# yt-dlp and deno keep their caches under HOME; /data holds the database, audio cache and bans.
RUN groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app && \
    install -d -o app -g app /data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DENO_NO_UPDATE_CHECK=1 \
    DB_PATH=/data/music_bot.db

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY . .
COPY --from=frontend /frontend/out ./activity-frontend/out
RUN python -m compileall -q -x '/\.venv/' /app

USER app
# Relative paths (audio_cache/, banned_users.txt) resolve here, inside the data volume.
WORKDIR /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=2m --start-interval=5s --retries=3 \
    CMD ["python", "/app/scripts/docker_healthcheck.py"]

ENTRYPOINT ["python", "/app/scripts/docker_entrypoint.py"]
CMD ["python", "/app/main.py"]
