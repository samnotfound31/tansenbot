# ─────────────────────────────────────────────────────────────────────────────
# Tansen Discord Music Bot — Production Dockerfile
# Build:  docker build -t tansen-bot .
# Run:    docker-compose up -d   (recommended)
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

LABEL maintainer="Tansen Bot"
LABEL description="Tansen Discord Music Bot — Lavalink + SoundCloud"

# ── System dependencies ───────────────────────────────────────────────────────
# curl : health checks
# No ffmpeg, no nodejs — Lavalink handles all audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

# ── Start the bot ─────────────────────────────────────────────────────────────
CMD ["python", "-u", "run.py"]
