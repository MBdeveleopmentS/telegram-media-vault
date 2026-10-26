#!/usr/bin/env python3
"""Print freshly generated secrets in .env format. Stdlib only.

    python3 scripts/gen_env_secrets.py >> .env

Store SESSION_ENCRYPTION_KEYS separately from database backups: anyone with
both can decrypt user sessions.
"""

import base64
import os
import secrets


def tok(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


print("APP_SECRET_KEY=" + tok(48))
print("SESSION_ENCRYPTION_KEYS=k1:" + base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="))
print("SESSION_ENCRYPTION_ACTIVE_KEY_ID=k1")
print("WEBHOOK_SECRET=" + tok(32))
for name in (
    "REDIS_PASSWORD",
    "POSTGRES_OWNER_PASSWORD",
    "VAULT_BOT_DB_PASSWORD",
    "VAULT_AUTHWEB_DB_PASSWORD",
    "VAULT_ENGINE_DB_PASSWORD",
    "VAULT_SCHEDULER_DB_PASSWORD",
):
    print(f"{name}={tok(24)}")
