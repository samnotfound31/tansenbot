# ─────────────────────────────────────────────────────────────────────────────
# Tansen Discord Music Bot — Production Dockerfile
# Build:  docker build -t tansen-bot .
# Run:    docker-compose up -d   (recommended)
# ─────────────────────────────────────────────────────────────────────────────

# Use official Python slim image — smaller than full, still has everything needed
FROM python:3.11-slim

# Metadata
LABEL maintainer="Tansen Bot"
LABEL description="Tansen Discord Music Bot with FFmpeg and yt-dlp"

# ── System dependencies ───────────────────────────────────────────────────────
# ffmpeg  : audio streaming     (replaces the Windows C:\ffmpeg path)
# curl    : health checks
# gcc     : needed by some pip packages on slim images
# nodejs  : required by yt-dlp for JavaScript evaluation (YouTube bot-detection bypass)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    gcc \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Install Python dependencies ───────────────────────────────────────────────
# Copy requirements first so Docker caches this layer (faster rebuilds)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Copy bot source code ──────────────────────────────────────────────────────
# .dockerignore prevents .env, *.db, downloads/, cookies.txt from being baked in
COPY . .

# ── Create persistent data directory ─────────────────────────────────────────
# The /data volume will be mounted from the host so DB survives container restarts
RUN mkdir -p /data

# ── Expose keep-alive port ────────────────────────────────────────────────────
EXPOSE 8080

# ── Health check ─────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

# ── Start the bot ─────────────────────────────────────────────────────────────
CMD ["python", "-u", "tansenmain.py"]
