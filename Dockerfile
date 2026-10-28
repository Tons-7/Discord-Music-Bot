# Images are pinned by tag and digest; Dependabot's docker ecosystem bumps both together.

### Stage 1: Build Next.js frontend
FROM node:26.11.1-slim@sha256:193fe51b64e77981119c98c2002c9e32a70e2f006fb4d25068ce0558998917f0 AS frontend
WORKDIR /frontend
COPY activity-frontend/package.json activity-frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY activity-frontend/ .
RUN npm run build

# JS runtime for yt-dlp's YouTube signature solving
FROM denoland/deno:bin-2.9.7@sha256:bc5aa4466e21b6d3021226a85ba2e1911f7c386254d97b9d797903ab74edace2 AS deno

# Keep in step with the uv version pinned in .github/workflows/ci.yml.
FROM ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b AS uv

### Stage 2: Python dependencies (pyproject.toml + uv.lock)
FROM python:3.14.8-slim-trixie@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a AS builder
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
FROM python:3.14.8-slim-trixie@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a

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
