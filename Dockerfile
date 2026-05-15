FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    NODE_BIN=/usr/bin/node \
    PIP_NO_CACHE_DIR=1

# System deps
#  ffmpeg          → audio/video work (includes libass for caption burn)
#  nodejs          → yt-dlp-ejs n-challenge solver for YouTube
#  fonts-*         → caption rendering for Latin + Indic scripts + Montserrat
#  libsm6/libxext6 → OpenCV runtime (used by speaker_focus)
#  ca-certificates → HTTPS for yt-dlp / Supabase / OpenRouter
#  curl            → healthcheck convenience
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    fonts-noto \
    fonts-noto-core \
    fonts-noto-cjk \
    fonts-indic \
    fonts-noto-color-emoji \
    fonts-montserrat \
    libsm6 \
    libxext6 \
    libgl1 \
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

# Default CMD runs the web. The worker service should override with:
#   CMD ["celery", "-A", "celery_app", "worker", "--loglevel=INFO", "--concurrency=2"]
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
