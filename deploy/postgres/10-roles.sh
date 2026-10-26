#!/bin/sh
# Creates one least-privilege login role per component. Runs once, on first DB init.
# Table-level grants are applied after migrations by `vault apply-grants`.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE vault_bot       LOGIN PASSWORD '${VAULT_BOT_DB_PASSWORD}';
CREATE ROLE vault_authweb   LOGIN PASSWORD '${VAULT_AUTHWEB_DB_PASSWORD}';
CREATE ROLE vault_engine    LOGIN PASSWORD '${VAULT_ENGINE_DB_PASSWORD}';
CREATE ROLE vault_scheduler LOGIN PASSWORD '${VAULT_SCHEDULER_DB_PASSWORD}';
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO vault_bot, vault_authweb, vault_engine, vault_scheduler;
SQL
