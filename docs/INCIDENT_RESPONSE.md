# Incident response (operators)

1. **Contain:** `docker compose stop engine` (stops all MTProto activity) or `make down`.
2. **Suspected `.env` or session-key leak:** rotate `SESSION_ENCRYPTION_KEYS`. Add a new key id, set it active, and restart the engine; it re-seals sessions on connect. Then remove the old key. If sessions themselves may be stolen, ask users to terminate "Media Vault" under Telegram → Settings → Devices and reconnect.
3. **Bot token leak:** `/revoke` in @BotFather, update `BOT_TOKEN`, restart.
4. **DB leak without keys:** rotate DB passwords; sessions stay protected by encryption, but treat chat titles and ids as exposed.
5. **Evidence:** keep container logs (already redacted) and the `security_events` table.
6. **Notify** affected users through the bot with a factual description.
