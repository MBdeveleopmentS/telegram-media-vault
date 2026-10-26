.PHONY: up down logs ps test test-pg lint typecheck check stats crash-engine

COMPOSE ?= docker compose

up:            ## build and start everything (the one development command)
	@test -f .env || (echo "missing .env — run: cp .env.example .env && python3 scripts/gen_env_secrets.py >> .env" && exit 1)
	$(COMPOSE) build migrate
	$(COMPOSE) up -d --remove-orphans
	$(COMPOSE) ps

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100 bot engine

ps:
	$(COMPOSE) ps

stats:         ## queue lengths and job state counts (no personal data)
	$(COMPOSE) exec -e DATABASE_URL="postgresql+asyncpg://vault_engine:$$(grep ^VAULT_ENGINE_DB_PASSWORD .env | cut -d= -f2-)@postgres:5432/vault" engine python -m vault.cli queue-stats

crash-engine:  ## simulate a worker crash (SIGKILL, no graceful shutdown)
	$(COMPOSE) kill -s SIGKILL engine && $(COMPOSE) up -d engine

test:
	.venv/bin/python -m pytest -q

test-pg:       ## run the suite against a throwaway PostgreSQL container
	./scripts/test_postgres.sh

lint:
	.venv/bin/ruff check src tests migrations && .venv/bin/ruff format --check src tests migrations

typecheck:
	.venv/bin/mypy

check: lint typecheck test
