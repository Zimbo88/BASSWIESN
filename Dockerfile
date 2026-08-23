FROM python:3.12-slim

WORKDIR /app
ARG APP_UID=10001
ARG APP_GID=10001
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client openssl sshpass \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" basswiesn \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin basswiesn
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY basswiesn ./basswiesn
COPY tools ./tools
COPY docs ./docs
COPY FEATURES.md RELEASE_CHECKLIST.md ./
RUN mkdir -p /app/data /app/tmp /app/secrets/setup-rebuild \
    && touch /app/secrets/setup-rebuild/known_hosts \
    && chown -R basswiesn:basswiesn /app
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 1328 1329 1516 1860
USER basswiesn
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["python", "-c", "import json, urllib.request; payload=json.load(urllib.request.urlopen('http://127.0.0.1:1328/api/readiness', timeout=3)); raise SystemExit(0 if payload.get('ready') else 1)"]
CMD ["python", "tools/run_dev.py"]
