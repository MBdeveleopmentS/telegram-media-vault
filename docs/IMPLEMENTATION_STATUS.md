# Implementation status

Legend: **IMPLEMENTED** code exists · **TESTED** automated tests (fakes) · **LIVE** verified against real Telegram · **MOCKED** only fake paths · **MISSING**

| Area | Status |
|---|---|
| Config validation, per-component secrets, production guards | IMPLEMENTED, TESTED |
| PostgreSQL schema + Alembic migration (upgrade/downgrade checked) | IMPLEMENTED, TESTED |
| Least-privilege DB roles and grants | IMPLEMENTED (exercised in compose) |
| Session encryption (AES-256-GCM, AAD-bound, key rotation) | IMPLEMENTED, TESTED |
| In-chat login (contact, keypad, 2FA) | IMPLEMENTED, TESTED with fakes, **LIVE** (one account) |
| HTTPS login page (optional) | IMPLEMENTED, TESTED |
| Chat selection (forward / list / search, re-verification) | IMPLEMENTED, TESTED, LIVE |
| Listener → durable job → worker → Bot API delivery → cleanup | IMPLEMENTED, TESTED, **LIVE** (photos) |
| Videos, >5 MiB skip, two users, disconnect, FloodWait | TESTED with fakes; **LIVE pending** |
| Crash and Redis-loss recovery, ambiguous upload handling | IMPLEMENTED, TESTED |
| Telegram Stars payments, renewal, refunds, reminders, downgrade | IMPLEMENTED, TESTED with fakes; **LIVE pending** |
| Album delivery as a single media group | MISSING (album items are delivered individually) |
| Historical import | MISSING (out of scope for V1) |
| External security audit | **NOT DONE** |

## Known limitations
- At-least-once delivery: rare duplicates after a crash mid-upload are possible and labelled.
- Messages older than `CATCHUP_MAX_MESSAGES` per chat that arrive during downtime are not backed up.
- Deleting temp files is not forensic erasure; tmpfs pages can reach swap.
- A single engine shard handles all accounts by default.
