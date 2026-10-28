# Images are pinned by tag and digest; Dependabot's docker ecosystem bumps both together.

### Stage 1: Build Next.js frontend
FROM node:26.12.0-slim@sha256:e556a144463cc8dc53afe8de6d2fa0db5c132fd39ffa232c17641bd40df2bf11 AS frontend
WORKDIR /frontend
COPY activity-frontend/package.json activity-frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY activity-frontend/ .
RUN npm run build

# JS runtime for yt-dlp's YouTube signature solving
FROM denoland/deno:bin-2.9.7@sha256:bc5aa4466e21b6d3021226a85ba2e1911f7c386254d97b9d797903ab74edace2 AS deno

# Keep in step with the uv version pinned in .github/workflows/ci.yml.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

### Stage 2: Python dependencies (pyproject.toml + uv.lock)
FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder
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
FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

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
