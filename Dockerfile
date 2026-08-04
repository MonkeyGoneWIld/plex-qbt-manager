FROM python:3.12-slim

LABEL org.opencontainers.image.title="plex-qbt-manager" \
      org.opencontainers.image.description="Toggles qBittorrent alternative speed limits based on remote Plex playback" \
      org.opencontainers.image.source="https://github.com/MonkeyGoneWIld/plex-qbt-manager" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HTTP_PORT=5252

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Non-root; /app/logs is owned by it so an optional bind mount is writable
RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /app/logs \
    && chown -R app:app /app
USER app

EXPOSE 5252

# /health answers 503 when degraded, which raises HTTPError and exits non-zero
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ['HTTP_PORT']+'/health',timeout=5)"

CMD ["python", "app.py"]
