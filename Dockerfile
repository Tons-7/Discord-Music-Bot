### Stage 1: Build Next.js frontend
FROM node:20-slim AS frontend
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

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Copy built frontend from stage 1
COPY --from=frontend /frontend/out ./activity-frontend/out

CMD ["python", "main.py"]
