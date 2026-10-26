# Privacy

This document describes what the software stores when deployed as published. An operator who modifies the code or configuration may store more. Ask the operator of the instance you use.

## What connecting an account means

The bot logs in to your Telegram account as a *user session* (MTProto). Telegram grants such a session broad technical access to the whole account. **Telegram offers no way to restrict it to specific chats.** The code restricts itself:

- Messages from chats you did not approve are dropped by an in-memory check before any database access.
- Each job is re-checked against your approval and your account status before download.
- Media is only ever sent to your own private chat with the bot. The worker refuses to deliver if the account owner and the bot user differ.

## Stored data

| Data | Purpose | Protection |
|---|---|---|
| Telegram user id | identify you | — |
| Encrypted user session | keep the account connected | AES-256-GCM; the key lives outside the database |
| Titles of chats you approved | show them to you in the bot | — |
| Per-file job metadata (chat id, message id, media type, size, status) | queueing, duplicate prevention, status screen | deleted after 90 days |
| Backup counters per chat | status screen | kept while the chat is configured |
| Payment records (Telegram charge id, amount) | Premium entitlement, refunds | kept for accounting |
| Security events (no secrets, no content) | abuse detection | deleted after 180 days |

## Not stored

- **Media content.** It exists only in a RAM-backed temporary directory during transfer, and is deleted after delivery, after a terminal failure, or by the crash sweeper. Deletion is an ordinary file deletion, not forensic erasure. RAM pages may reach swap if the host swaps.
- Phone numbers (only a keyed hash, for rate limiting), login codes, 2FA passwords, and message text.

## Where your backups live

Delivered files are messages in your chat with the bot, so they live in **Telegram's cloud**. This is not an independent off-platform backup. If you delete that chat, the copies are gone.

## Disconnecting

`/disconnect` stops monitoring immediately. Disconnecting is recorded in the database first, so no new media is processed even if other components lag. The engine then logs the session out at Telegram and deletes the encrypted session, normally within seconds. If the engine is down at that moment, this happens when it starts again. A fallback purge deletes the ciphertext at the latest 10 minutes after that. Verify under **Telegram → Settings → Devices** that "Media Vault" is gone. Delivered files remain in your chat. Paused chat configurations are kept so you can reconnect.

## Telemetry

None. The software sends no analytics anywhere. Logs are local, structured, and pass through a redaction filter for tokens, session strings, phone numbers, and codes.
