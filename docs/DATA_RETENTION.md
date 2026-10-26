# Data retention

| Data | Retention |
|---|---|
| Media bytes | Only during transfer (RAM tmpfs). Removed on success, terminal failure, or retry; crash sweeper after 1 h |
| Job metadata (terminal) | 90 days, then deleted by the scheduler. Per-chat counters remain |
| Authentication flows (web) | 30 days |
| Security events | 180 days |
| Encrypted session | Until disconnect or expiry. Purged within seconds of disconnect (fallback ≤10 min) |
| Monitored chat configs | Until the user removes them (kept, paused, across disconnects and downgrades) |
| Payments | Kept for accounting and refunds |
| Redis | Transient: queue ids, rate-limit counters, RPC entries (TTL ≤60 s) |
