# Testing

```bash
make check     # ruff, mypy, pytest (SQLite + fakeredis)
make test-pg   # pytest against a disposable PostgreSQL 17 container (+ alembic upgrade/check/downgrade)
```

- `tests/unit`: plans, subscriptions, job state machine, retry policy, media classification, filename hygiene, queue fairness, crypto, temp files, config, Telethon TL conversion.
- `tests/integration`: full pipeline with fake MTProto source and Bot API, crash and Redis recovery, burst and two-tenant runs, login (web and in-chat), bot handlers through a real aiogram Dispatcher, payments and lifecycle.
- `tests/security`: cross-tenant access, tampered jobs, queue poisoning, RPC isolation, log redaction, webhook secret, security-event allow-list.

No real Telegram credentials are used. Live tests are manual and must use accounts and chats you own. See `docs/PENTEST_GUIDE.md`.
