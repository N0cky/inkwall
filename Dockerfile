FROM python:3.13-slim

LABEL org.opencontainers.image.title="Inkwall" \
      org.opencontainers.image.description="Self-hosted image server for E-Ink now playing and idle dashboards." \
      org.opencontainers.image.licenses="LicenseRef-Inkwall-NonCommercial"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    INKWALL_CONFIG_FILE=/config/settings.env \
    INKWALL_OUTPUT_DIR=/output \
    INKWALL_LOGS_DIR=/logs

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN adduser --disabled-password --gecos "" appuser

COPY app/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Der Programmcode gehört root und bleibt für den laufenden Server schreibgeschützt;
# schreiben darf er nur in /output, /logs und /config (Volumes)
COPY . /app

RUN mkdir -p /output /logs /config \
    && chown appuser:appuser /output /logs /config \
    && chmod 755 /app/entrypoint.sh

EXPOSE 8787

# start-period großzügig: das Initial-Render kann bei nicht erreichbarem Plex über 60 s dauern
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3)"

ENTRYPOINT ["/app/entrypoint.sh"]
# 1 Worker (Render-Thread lebt im Prozess), 4 Threads (ESP32-Polls blockieren nicht hinter dem UI),
# 120 s Timeout (langsame Requests killen nicht den Prozess samt Render-Thread)
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "120", "--bind", "0.0.0.0:8787", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
