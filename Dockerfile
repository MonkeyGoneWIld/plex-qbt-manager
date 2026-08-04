# Alpine rather than Debian slim: python:3.12-slim carried ~170 CVEs in base
# packages this service never uses (perl-base, util-linux, login, apt, tar,
# ncurses), none of which Debian has shipped fixes for. Alpine doesn't include
# them at all. 3.13 also clears the ~28 CVEs against CPython 3.12.13.
FROM python:3.13-alpine

LABEL org.opencontainers.image.title="plex-qbt-manager" \
      org.opencontainers.image.description="Toggles qBittorrent alternative speed limits based on remote Plex playback" \
      org.opencontainers.image.source="https://github.com/MonkeyGoneWIld/plex-qbt-manager" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HTTP_PORT=5252

WORKDIR /app

# setuptools is upgraded (the shipped 70.3.0 carries CVE-2025-47273) and pip is
# deleted afterwards: nothing imports it at runtime, and its vendored msgpack
# copy was the only thing pulling GHSA-6v7p-g79w-8964 into the image.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade setuptools \
    && pip install --no-cache-dir -r requirements.txt \
    && SITE="$(python -c 'import site; print(site.getsitepackages()[0])')" \
    && rm -rf "$SITE"/pip "$SITE"/pip-*.dist-info \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.* \
    && if python -c "import pip" 2>/dev/null; then echo "pip survived removal"; exit 1; fi

COPY app.py .

# uid pinned to 1000 so an existing bind-mounted logs dir stays writable
RUN adduser -D -u 1000 app \
    && mkdir -p /app/logs \
    && chown -R app:app /app
USER app

EXPOSE 5252

# /health answers 503 when degraded, which raises HTTPError and exits non-zero.
# Exec form so this never invokes busybox sh.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ['HTTP_PORT']+'/health',timeout=5)"]

CMD ["python", "app.py"]
