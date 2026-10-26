# Design decisions

| # | Decision | Why | Trade-off |
|---|---|---|---|
| D1 | Telethon 1.45 for MTProto, aiogram 3.31 for the bot | Both maintained and widely used; aiogram supports Stars payments | Two Telegram stacks |
| D2 | Modular monolith, one image, two processes by default | Small-VPS friendly | Shared fate within the engine |
| D3 | Login and listener and downloads in one process per account | Avoid `AUTH_KEY_DUPLICATED`; only one place holds session keys | Engine sharding (`ENGINE_SHARDS`) needed to scale |
| D4 | **In-chat login** (share-contact button, inline code keypad, 2FA message deleted) | Product owner preference over an external page. The keypad avoids Telegram invalidating codes sent in chats | The 2FA password briefly exists as a chat message before deletion. The HTTPS page remains as an option (`--profile web`) |
| D5 | Identity binding: the MTProto user id must equal the bot user id | Bot API and MTProto share user ids. Prevents connecting someone else's account | Users can't connect a second account |
| D6 | Media never uses a direct MTProto→Bot API file-id shortcut | MTProto file references are not Bot API file ids. Forwarding as the user would act in the user's outbox and leak "forwarded from" metadata | Every file is downloaded once |
| D7 | Temp storage is a RAM tmpfs with fixed `payload` file names | No disk persistence by default; no remote-controlled paths | RAM use, bounded by tmpfs size; possible swap exposure |
| D8 | Protected-content chats and messages are skipped and cannot be added | Respect Telegram restrictions | — |
| D9 | Premium = 90-day prepaid Stars package, not an auto-renewing subscription | Product requirement | Users must renew manually (reminders at 7/3/1 days) |
| D10 | On downgrade, keep chats marked "📌 keep on Free" first, then the oldest-added. Pause the rest; delete nothing; notify with the exact lists | Deterministic and announced. **Product owner may change the fallback rule** | — |
| D11 | Oversized media is skipped from metadata and never retried after an upgrade | Product requirement | — |
| D12 | Codes and passwords pass through Redis only as transient RPC entries consumed within milliseconds, never stored or logged | Login runs in the engine, not the bot | Redis must be private and password-protected (default compose: internal network only) |
| D13 | Operator lifetime Premium via `vault grant-lifetime` (expiry 9999-12-31) | Admin and team accounts | No payment row; visible only in the DB |
