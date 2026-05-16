FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    NODE_BIN=/usr/bin/node \
    PIP_NO_CACHE_DIR=1 \
    CELERY_CONCURRENCY=2 \
    CELERY_POOL=threads \
    CLIP_RENDER_WORKERS=1 \
    ANALYZER_CHUNK_WORKERS=2

# System deps
#  ffmpeg          → audio/video work (includes libass for caption burn)
#  nodejs          → yt-dlp-ejs n-challenge solver for YouTube
#  fonts-*         → caption rendering for Latin + Indic scripts
#  tini            → proper PID 1 init for signal forwarding + zombie reaping
#  ca-certificates → HTTPS for yt-dlp / Supabase / OpenRouter
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    fonts-noto \
    fonts-noto-cjk \
    fonts-indic \
    fonts-noto-color-emoji \
    tini \
    ca-certificates \
    curl \
    git \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN chmod +x /app/start.sh \
    && mkdir -p /app/storage/jobs

# Railway/Fly/Render inject PORT. Default 8000 locally.
ENV PORT=8000
EXPOSE 8000

# tini handles SIGTERM/SIGINT cleanly and reaps zombies.
# start.sh runs celery + uvicorn together and exits if either dies.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
