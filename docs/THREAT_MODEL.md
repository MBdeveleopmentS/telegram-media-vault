# Threat model

Assets: user MTProto sessions (the crown jewels), media in transit, chat metadata, payment entitlements.
Trust boundaries: Telegram ↔ bot/engine; bot ↔ engine (Redis RPC); processes ↔ PostgreSQL; host ↔ containers.

| # | Threat | Mitigation | Residual risk |
|---|---|---|---|
| 1 | Database breach | Sessions encrypted with AES-256-GCM; key outside the DB; AAD binds ciphertext to the account row; no phone, code, password or media stored | DB + `.env` together = full compromise |
| 2 | Session theft | Only the engine (and optional authweb) can decrypt; per-role DB grants; bot role can't read sessions | Host root compromise |
| 3 | Credential leakage | Secrets via env only; redacting logger; `.env` git-ignored; per-service env | Operator mistakes |
| 4 | Compromised admin | No admin UI exposes sessions; CLI prints no secrets | Shell access = full access |
| 5 | Webhook spoofing | `secret_token` header verified (aiogram); polling by default | — |
| 6 | Payment update replay | Unique `telegram_payment_charge_id`; PENDING→PAID transition; update_id dedupe | — |
| 7–9 | Cross-tenant IDOR (jobs, chats, dialog lists) | Internal user always derived from the update sender; every query scoped by `user_id`; chat ids re-verified against the caller's own dialogs; RPC resolves only the caller's account | Tested in `tests/security` |
| 10 | Malicious filenames / traversal | Payload path is a fixed name in a random 0700 dir; display names sanitized (controls, bidi, extension forced from MIME) | — |
| 11 | Oversized media | Metadata precheck plus a hard byte cap while streaming | — |
| 12–13 | Decompression bombs, malformed media | No decoding or transcoding at all; bytes are relayed as-is | Telegram clients render the content |
| 14 | Flooding / DoS | Rate limits (login per user/phone/IP), per-user backlog cap, bounded inbox, pools, memory limits | Volumetric attacks on the host |
| 15 | Worker starvation | Weighted round-robin with fallback; per-user concurrency | — |
| 16 | Queue poisoning | Redis carries ids only; non-integers dropped; claims are conditional DB updates; shard checked | Redis write access = DoS only |
| 17 | DB connection exhaustion | Small bounded pools, `max_connections=40` | — |
| 18 | Redis disruption | PostgreSQL is the truth; lost ids re-pushed; enqueue failures don't lose jobs | Temporary delay |
| 19 | Host crash mid-upload | Lease expiry + `delivery_ambiguous` labelling | Possible duplicate |
| 20 | Expired sessions | Detected, account marked EXPIRED, user notified, monitoring stops | — |
| 21 | Wrong account-to-bot binding | MTProto id must equal bot user id; mismatch is logged out and recorded; worker re-checks before delivery | — |
| 22 | Public exposure of temp files | RAM tmpfs inside the container, 0700, never served by any web route | — |
| 23–24 | Supply chain / images | Hash-pinned lockfiles, digest-pinned images, pip-audit + Trivy + Dependabot in CI | Upstream compromise before detection |
| 25 | Log privacy leaks | Key- and value-based redaction; access logs off; tests assert absence | Novel secret formats |
| 26 | Wrong downgrade | Deterministic, user-driven selection; nothing deleted; user notified with lists | — |
| 27 | Leaked login flow tokens | (web flow) single-use, 10-min TTL, exchanged for an HttpOnly cookie; (chat flow) bound to the update sender; own contact required | — |
| 28 | Debug endpoints | Docs/OpenAPI disabled; DEBUG logging refused in production | — |
| 29 | Protected content | Skipped at ingest and download; protected chats can't be added | — |
| 30 | Revocation / deletion | `/disconnect` → DB first → Telegram log-out → ciphertext purge (fallback ≤10 min) | Engine down = delayed log-out |

## Additional threats
- **Login code phishing through the bot:** codes are entered on a keypad bound to the sender. The bot deletes anything that looks like a pasted code and warns the user.
- **Someone starting a login for another person's number:** rejected, because only the sender's own shared contact is accepted. Phone-hash rate limits also apply.
- **2FA password in chat history:** deleted immediately. Telegram still processed it as a message (accepted risk, D4).
- **Operator abuse:** the operator of an instance can modify the code. Users must trust the operator; open source does not change that.
