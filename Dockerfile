ARG PYTHON_BASE=python:3.12.14-alpine3.24@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111
FROM ${PYTHON_BASE} AS ssh-client-build
RUN apk upgrade --no-cache && apk add --no-cache build-base zlib-dev openssl-dev gnupg linux-headers
COPY tools/build_ssh_client.py tools/openssh-source.json /build/
RUN python /build/build_ssh_client.py --lock /build/openssh-source.json --output /openssh-client

FROM ${PYTHON_BASE}

WORKDIR /app
ARG BASSWIESN_VERSION=5.0.0
LABEL org.opencontainers.image.version="${BASSWIESN_VERSION}" \
      org.opencontainers.image.source="https://github.com/Zimbo88/BASSWIESN"
ARG APP_UID=10001
ARG APP_GID=10001
RUN apk upgrade --no-cache && apk add --no-cache openssl sshpass bash tzdata \
    && addgroup -S -g "${APP_GID}" basswiesn \
    && adduser -S -D -H -u "${APP_UID}" -G basswiesn -h /tmp -s /sbin/nologin basswiesn
COPY --from=ssh-client-build /openssh-client/ /usr/local/
COPY requirements.txt requirements.lock ./
# Dependencies are installed only at build time. Updates replace the image;
# the running service needs no package installer or its archive-extraction code.
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt && pip check \
    && pip uninstall --yes pip
COPY basswiesn ./basswiesn
RUN python -c 'import sys; from basswiesn import __version__; assert __version__ == sys.argv[1], "OCI/package version mismatch"' "$BASSWIESN_VERSION"
COPY tools ./tools
RUN chmod 755 /app/tools/basswiesnctl \
    && ln -s /app/tools/basswiesnctl /usr/local/bin/basswiesnctl
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
HEALTHCHECK --interval=30s --timeout=8s --start-period=30s --retries=3 CMD ["python", "tools/run_dev.py", "--healthcheck"]
CMD ["python", "tools/run_dev.py"]
