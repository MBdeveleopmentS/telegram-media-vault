#!/usr/bin/env bash
# Runs the full test suite against a disposable PostgreSQL container (localhost only).
set -euo pipefail
NAME=mv-test-pg-$$
PORT=${PORT:-55439}
docker run -d --rm --name "$NAME" -e POSTGRES_USER=t -e POSTGRES_PASSWORD=t -e POSTGRES_DB=t \
  -p 127.0.0.1:${PORT}:5432 postgres:17.6-alpine >/dev/null
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do docker exec "$NAME" pg_isready -U t >/dev/null 2>&1 && break; sleep 1; done
sleep 1
export DATABASE_URL="postgresql+asyncpg://t:t@127.0.0.1:${PORT}/t"
.venv/bin/alembic upgrade head
.venv/bin/alembic check
.venv/bin/alembic downgrade base
TEST_DATABASE_URL="$DATABASE_URL" .venv/bin/python -m pytest -q "$@"
