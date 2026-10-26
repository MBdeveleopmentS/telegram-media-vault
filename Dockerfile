# syntax=docker/dockerfile:1.7
# Single image, one process per container (bot | authweb | engine | scheduler | migrate).
# Base images are pinned by digest; Python deps are installed with --require-hashes.

ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes --no-deps -r /tmp/requirements.lock

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="telegram-media-vault" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/mosheisraelov01-gif/telegram-media-vault"
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app/src PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    VAULT_RUN_DIR=/tmp/vault-run
RUN groupadd --system --gid 10001 vault \
 && useradd --system --uid 10001 --gid vault --home-dir /nonexistent --shell /usr/sbin/nologin vault \
 && install -d -o vault -g vault -m 0700 /var/lib/vault /var/lib/vault/tmp
COPY --from=build /opt/venv /opt/venv
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY src /app/src
WORKDIR /app
USER 10001:10001
CMD ["python", "-c", "print('choose a component: python -m vault.bot.main | vault.authweb.main | vault.engine.main | vault.scheduler.main')"]
