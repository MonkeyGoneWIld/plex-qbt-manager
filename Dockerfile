FROM python:3.12-slim

LABEL org.opencontainers.image.title="plex-qbt-manager" \
      org.opencontainers.image.description="Toggles qBittorrent alternative speed limits based on remote Plex playback" \
      org.opencontainers.image.source="https://github.com/MonkeyGoneWIld/plex-qbt-manager" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HTTP_PORT=5252

WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app.py healthcheck.py ./

# Non-root user; logs dir is owned by it so the optional bind mount can be written
RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /app/logs \
    && chown -R app:app /app
USER app

EXPOSE 5252

# Pure-Python healthcheck — no curl needed in the image
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "/app/healthcheck.py"]

CMD ["python", "app.py"]
