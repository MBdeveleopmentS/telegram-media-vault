# Telegram Media Vault

A self-hostable Telegram bot that automatically backs up **new photos and videos** from chats you select into your private conversation with the bot. The user interface is in Hebrew.

> **Read this before connecting an account.** Connecting a Telegram account creates a full *user session*. Telegram does not support chat-scoped authorization for user sessions, so technically the session can reach every chat on the account. This software is written to process **only chats you explicitly approve**. Open source lets you check that claim; it does not guarantee safety. **No independent security audit has been done.**

## How it works

```
your chat ──MTProto (Telethon)──▶ engine: listener ─▶ PostgreSQL job ─▶ Redis queue
                                                        │
                                            worker: download to RAM tmpfs
                                                        │
                                 Bot API upload ─▶ your private chat with the bot ─▶ temp copy deleted
```

1. `/start` the bot and tap **🔗 חיבור חשבון** (Connect account).
2. Share your own phone number with the button. The bot accepts only your own contact.
3. Type the login code on the in-chat keypad. The code is never sent as a chat message, because Telegram invalidates codes that are sent in chats. If you use 2FA, the bot asks for your password and deletes that message immediately.
4. Pick a chat: forward a message from it, or choose it from your chat list.
5. New photos and videos in that chat now arrive in your bot conversation.

Delivered media is stored in **Telegram's cloud**, in your bot conversation. This is not an independent backup outside Telegram.

## Plans

| | Free | Premium |
|---|---|---|
| Monitored chats | 3 | 10 |
| Max file size | 5 MiB | 15 MiB |
| Queue priority | standard | weighted 3:1 |
| Price | free | 150 ⭐ per 90 days (prepaid, no auto-renewal) |

All limits and the price are server-side settings (`.env`).

## Quick start (self-hosting)

Requirements: Docker with the Compose plugin, a bot token from [@BotFather](https://t.me/BotFather), and an `api_id` and `api_hash` from <https://my.telegram.org>.

```bash
git clone https://github.com/mosheisraelov01-gif/telegram-media-vault.git
cd telegram-media-vault
cp .env.example .env
python3 scripts/gen_env_secrets.py >> .env   # random DB/Redis passwords + encryption keys
$EDITOR .env                                  # BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, SUPPORT_CONTACT
make up
```

The default deployment runs **two application processes** (`bot` and `engine`) plus PostgreSQL and Redis. It fits on a small VPS: memory limits total about 1.3 GB, including a 192 MB RAM-backed scratch area for media.

**Back up `.env` somewhere safe.** It contains the session encryption key. Without it, every user must reconnect. Never store it next to database backups.

Useful commands:

```bash
make logs        # bot + engine logs (secrets are redacted)
make stats       # queue lengths and job-state counts (no personal data)
make down
docker compose run --rm migrate python -m vault.cli grant-lifetime <telegram_user_id>
docker compose run --rm migrate python -m vault.cli refund <payment_id>
```

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock && pip install --no-deps -e .
make check       # ruff + mypy + pytest (SQLite + fakeredis, no Telegram credentials needed)
make test-pg     # same suite against a throwaway PostgreSQL container
```

Tests use fake Telegram clients. No real account or token is needed. See [docs/TESTING.md](docs/TESTING.md).

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, delivery semantics
- [docs/DECISIONS.md](docs/DECISIONS.md): design decisions and trade-offs
- [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md): what is implemented, tested, mocked, or missing
- [SECURITY.md](SECURITY.md): how to report vulnerabilities
- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md): threats and mitigations
- [PRIVACY.md](PRIVACY.md) and [docs/DATA_RETENTION.md](docs/DATA_RETENTION.md): what is stored and for how long
- [docs/PENTEST_GUIDE.md](docs/PENTEST_GUIDE.md): what an external tester should examine

## License

[Apache-2.0](LICENSE). This project is not affiliated with Telegram.
