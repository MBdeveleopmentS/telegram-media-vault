# Architecture

A modular monolith: one codebase, one Docker image, and in the default deployment **two application processes**.

| Process | Holds | Does |
|---|---|---|
| `bot` (aiogram) | `BOT_TOKEN`, `APP_SECRET_KEY` | Hebrew UI, chat selection, payments (Telegram Stars). **Never** holds `API_HASH` or session keys. |
| `engine` (Telethon + workers) | `BOT_TOKEN`, `API_ID/HASH`, session keys, `APP_SECRET_KEY` | Account runtime and listener, transfer workers, crash recovery, dialog RPC, in-chat login service, scheduler (reminders, downgrades, digests, retention). |
| `authweb` (optional, profile `web`) | like engine, minus workers | HTTPS login page, if you prefer it to the in-chat login. |
| PostgreSQL | durable source of truth | users, accounts, encrypted sessions, chats, jobs, deliveries, payments, security events |
| Redis | coordination only | job-id queues, control pub/sub, RPC lists, rate limits. Losing Redis loses nothing durable. |

Each process connects with its own PostgreSQL role (`vault_bot`, `vault_engine`, …). Grants are defined in `vault.cli.GRANTS`; the bot role cannot read `encrypted_sessions` at all.

## Why login and downloads live in the engine
An MTProto auth key must not be used concurrently by several processes (Telegram may answer `AUTH_KEY_DUPLICATED`). So each account is owned by exactly one engine shard (`telegram_accounts.shard`), which both listens and downloads. The login service runs in the same process because it needs the same secrets. It releases the half-built client before the engine connects with the new session.

## Media pipeline
```
NewMessage ─▶ in-memory "approved chat?" check ─▶ inbox (bounded) ─▶ ingest():
   classify media ▸ re-check chat/account/tier in DB ▸ size precheck ▸ INSERT job (idempotent unique key)
   ▸ COMMIT ▸ RPUSH job id (Redis)
worker: pop (weighted 3:1 Premium/Free) ▸ conditional UPDATE claim (lease) ▸ re-fetch message
   (fresh file_reference, protected-content check) ▸ stream to RAM tmpfs with a hard byte cap
   ▸ state UPLOADING ▸ Bot API sendPhoto/sendVideo/sendDocument to the owner's private chat
   ▸ DELIVERED + delivery row ▸ temp dir removed (always, in `finally`)
```
Idempotency key: `(account_id, source_chat_id, source_message_id, media_key)`.

Job states: `DISCOVERED → QUEUED → DOWNLOADING → DOWNLOADED → UPLOADING → DELIVERED`, plus `RETRY_WAIT`, `SKIPPED`, `FAILED`. Transitions are enforced in `vault.domain.jobs`.

## Delivery semantics (honest version)
Telegram accepting an upload and our database commit are two separate systems, so **exactly-once delivery is impossible**. We aim for at-least-once with duplicates minimized:
- A crash before upload: lease expires and the job is retried. No duplicate.
- A crash during or after upload, before commit: the job is re-queued with `delivery_ambiguous=true`. The retried caption tells the user the file may be a duplicate.
- A late commit after the lease was reclaimed is still recorded ("late confirmation"), which prevents a second resend.

## Recovery
Every 15 s the engine reclaims expired leases, promotes due `RETRY_WAIT` jobs, and re-pushes `QUEUED` jobs that Redis no longer has (a Redis set tracks enqueued ids, popped atomically via Lua). After a restart, a bounded catch-up (`CATCHUP_MAX_MESSAGES`) fetches messages newer than each chat's `last_seen_message_id`.

## Fairness and backpressure
- Weighted round-robin 3:1 Premium/Free. When one queue is empty the other is served, so neither is starved.
- Per-user concurrency limit (`PER_USER_CONCURRENCY`). Excess jobs go back to the tail.
- Per-user backlog cap (`MAX_PENDING_JOBS_PER_USER`), bounded listener inbox, bounded DB pools.
- FloodWait and Bot API `retry_after` are scheduled as `RETRY_WAIT` instead of sleeping, and don't consume an attempt.
