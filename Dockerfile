### Stage 1: Build Next.js frontend
FROM node:24-slim AS frontend
WORKDIR /frontend
COPY activity-frontend/package.json activity-frontend/package-lock.json* ./
RUN npm ci
COPY activity-frontend/ .
RUN npm run build

### Stage 2: Python runtime
FROM python:3.14-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# JS runtime for yt-dlp's YouTube signature solving
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# uv manages the Python environment (pyproject.toml + uv.lock)
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /bin/

WORKDIR /app

# Never fetch a standalone interpreter; use this image's python3.14.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# Dependency layer: cached until pyproject.toml/uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY . .

# Copy built frontend from stage 1
COPY --from=frontend /frontend/out ./activity-frontend/out

CMD ["uv", "run", "--no-sync", "python", "main.py"]
