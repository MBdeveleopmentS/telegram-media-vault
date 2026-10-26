"""Operator CLI. Never prints session material, tokens or personal data.

vault gen-secrets            # print fresh APP_SECRET_KEY / SESSION_ENCRYPTION_KEYS values
vault check-config <component>
vault apply-grants           # least-privilege PostgreSQL grants (run as schema owner)
vault queue-stats            # queue lengths + job state counts (no personal data)
vault refund <payment_id>    # refund a Stars payment through the Bot API
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from datetime import UTC, datetime

from sqlalchemy import func, select, text

from vault.config import Component, settings
from vault.db.models import JobState, MediaJob, utcnow
from vault.db.session import Database
from vault.queue.redis_queue import JobQueue
from vault.security.crypto import generate_key_b64

# table -> privileges, per component role. Column lists restrict sensitive tables.
GRANTS: dict[str, list[str]] = {
    "vault_bot": [
        "SELECT, INSERT, UPDATE ON users, subscriptions, payments, authentication_flows",
        "SELECT, INSERT, UPDATE, DELETE ON monitored_chats",
        "SELECT, UPDATE ON telegram_accounts, media_jobs",
        "INSERT ON security_events",
        "SELECT (id) ON security_events",
        # No access at all to encrypted_sessions or media_deliveries.
    ],
    "vault_authweb": [
        "SELECT, UPDATE ON users, authentication_flows, monitored_chats",
        "SELECT, INSERT ON subscriptions",
        "SELECT, INSERT, UPDATE ON telegram_accounts, encrypted_sessions",
        "INSERT ON security_events",
        "SELECT (id) ON security_events",
    ],
    # Engine also hosts in-chat login (authweb grants) and the scheduler by default.
    "vault_engine": [
        "SELECT, UPDATE ON users, telegram_accounts, monitored_chats, subscriptions, payments",
        "INSERT ON subscriptions, telegram_accounts",
        "SELECT, INSERT, UPDATE, DELETE ON encrypted_sessions",
        "SELECT, INSERT, UPDATE, DELETE ON media_jobs, authentication_flows",
        "SELECT, INSERT ON media_deliveries",
        "SELECT, INSERT, DELETE ON security_events",
    ],
    "vault_scheduler": [
        "SELECT, UPDATE ON users, subscriptions, monitored_chats, payments",
        "SELECT, UPDATE, DELETE ON media_jobs, authentication_flows",
        "SELECT, DELETE ON security_events",
        "SELECT ON telegram_accounts",
        "SELECT (id, account_id) ON encrypted_sessions",
        "DELETE ON encrypted_sessions",
    ],
}


async def apply_grants() -> None:
    db = Database(settings().database_url)
    async with db.transaction() as session:
        roles = set(
            (
                await session.execute(text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'vault\\_%'"))
            ).scalars()
        )
        for role, grants in GRANTS.items():
            if role not in roles:
                print(f"skip {role}: role does not exist")
                continue
            await session.execute(text(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{role}"'))
            await session.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            await session.execute(text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"'))
            for g in grants:
                await session.execute(text(f'GRANT {g} TO "{role}"'))
            print(f"granted {role}")
    await db.dispose()


async def queue_stats() -> None:
    from vault.queue.redis_queue import make_redis

    s = settings()
    db = Database(s.database_url)
    redis = make_redis(s.redis_url)
    q = JobQueue(redis)
    for shard in range(s.engine_shards):
        print(f"shard {shard} redis lengths: {await q.lengths(shard)}")
    async with db.session() as session:
        rows = (await session.execute(select(MediaJob.state, func.count()).group_by(MediaJob.state))).all()
    for state, n in sorted(rows, key=lambda r: r[0].value):
        print(f"{state.value:<12} {n}")
    stuck = [r for r in rows if r[0] in (JobState.DOWNLOADING, JobState.UPLOADING)]
    if stuck:
        print("note: DOWNLOADING/UPLOADING jobs are in flight or awaiting lease recovery")
    await redis.aclose()
    await db.dispose()


LIFETIME_UNTIL = datetime(9999, 12, 31, tzinfo=UTC)


async def grant_lifetime(telegram_user_ids: list[int]) -> None:
    """Operator grant: Premium that never expires (no payment row is created)."""
    from vault.db.models import Tier
    from vault.domain.subscriptions import get_or_create_user, get_subscription

    db = Database(settings().database_url)
    try:
        async with db.transaction() as session:
            for tg_id in telegram_user_ids:
                user = await get_or_create_user(session, tg_id)
                sub = await get_subscription(session, user.id)
                sub.tier = Tier.PREMIUM
                sub.premium_expires_at = LIFETIME_UNTIL
                sub.reminders_sent = []
                print(f"lifetime premium granted to telegram user {tg_id}")
    finally:
        await db.dispose()


async def refund(payment_id: int) -> None:
    from aiogram import Bot

    from vault.payments.service import StarsProvider, refund_payment

    s = settings()
    s.require_for(Component.BOT)
    assert s.bot_token is not None
    bot = Bot(s.bot_token.get_secret_value())
    db = Database(s.database_url)
    try:
        async with db.transaction() as session:
            p = await refund_payment(session, StarsProvider(bot), payment_id, utcnow())
        print(f"refunded payment {p.id} ({p.amount} {p.currency})")
    finally:
        await bot.session.close()
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vault")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-secrets")
    cc = sub.add_parser("check-config")
    cc.add_argument("component", choices=[c.value for c in Component])
    sub.add_parser("apply-grants")
    sub.add_parser("queue-stats")
    gl = sub.add_parser("grant-lifetime")
    gl.add_argument("telegram_user_ids", type=int, nargs="+")
    rf = sub.add_parser("refund")
    rf.add_argument("payment_id", type=int)
    args = ap.parse_args(argv)

    if args.cmd == "gen-secrets":
        print(f"APP_SECRET_KEY={secrets.token_urlsafe(48)}")
        print(f"SESSION_ENCRYPTION_KEYS=k1:{generate_key_b64()}")
        print("SESSION_ENCRYPTION_ACTIVE_KEY_ID=k1")
        print(f"WEBHOOK_SECRET={secrets.token_urlsafe(32)}")
        return 0
    if args.cmd == "check-config":
        settings().require_for(Component(args.component))
        print("ok")
        return 0
    if args.cmd == "apply-grants":
        asyncio.run(apply_grants())
        return 0
    if args.cmd == "queue-stats":
        asyncio.run(queue_stats())
        return 0
    if args.cmd == "grant-lifetime":
        asyncio.run(grant_lifetime(args.telegram_user_ids))
        return 0
    if args.cmd == "refund":
        asyncio.run(refund(args.payment_id))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
