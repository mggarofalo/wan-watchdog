# syntax=docker/dockerfile:1

FROM python:3.12-alpine

# tzdata so TZ in the compose file actually affects log timestamps; without it
# every log line would read UTC regardless of what TZ is set to.
RUN apk add --no-cache tzdata

WORKDIR /app

COPY bgw320.py health.py watchdog.py selftest.py ./
COPY testdata/ ./testdata/

# Run unprivileged. The watchdog needs no special capabilities: it makes
# outbound HTTP requests and writes one small state file.
RUN adduser -D -u 10001 watchdog \
    && mkdir -p /data \
    && chown -R watchdog:watchdog /data /app

USER watchdog

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WATCHDOG_PORT=8080 \
    WATCHDOG_STATE_FILE=/data/state.json

EXPOSE 8080

# Reboot history lives here, so the rate limit survives a container restart.
VOLUME ["/data"]

# Docker's own view of health, independent of the watchdog's verdict about the
# network: this only asks whether the endpoint is being served.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import os,sys,urllib.request; \
p=os.environ.get('WATCHDOG_PORT','8080'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["python3", "/app/watchdog.py"]
