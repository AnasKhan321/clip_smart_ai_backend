FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    NODE_BIN=/usr/bin/node \
    PIP_NO_CACHE_DIR=1 \
    CELERY_CONCURRENCY=2

# System deps
#  ffmpeg          → audio/video work (includes libass for caption burn)
#  nodejs          → yt-dlp-ejs n-challenge solver for YouTube
#  fonts-*         → caption rendering for Latin + Indic scripts + Montserrat
#  libsm6/libxext6/libgl1 → OpenCV runtime (speaker_focus)
#  supervisor      → runs uvicorn + celery worker in the same container
#  ca-certificates → HTTPS for yt-dlp / Supabase / OpenRouter
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    fonts-noto \
    fonts-noto-cjk \
    fonts-indic \
    fonts-noto-color-emoji \
    libsm6 \
    libxext6 \
    libgl1 \
    supervisor \
    ca-certificates \
    curl \
    git \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/storage/jobs

# Render/Railway/Fly inject PORT. Default to 8000 locally.
ENV PORT=8000
EXPOSE 8000

# Single-container deploy: supervisord runs the API and the Celery worker
# together. Both processes share the container's RAM/CPU — if you hit OOMs
# either lower CELERY_CONCURRENCY or split into two services.
CMD ["supervisord", "-c", "/app/supervisord.conf"]
