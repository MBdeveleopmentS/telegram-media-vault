"""Authentication-flow service (shared by bot and auth-web).

Flow-token lifecycle:
  bot creates flow (state CREATED) and sends the user a one-time link containing
  a random token; only HMAC(token) is stored. Opening the link *consumes* the
  token: it's exchanged for an HttpOnly, SameSite=Strict browser cookie (whose
  HMAC is stored) and the browser is redirected to a URL without the token.
  A leaked link is therefore useless once opened, and useless after TTL.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vault.db.models import AuthenticationFlow, FlowState
from vault.security.crypto import Hasher, new_token

OPEN_STATES = frozenset({FlowState.CREATED, FlowState.OPENED, FlowState.CODE_SENT, FlowState.PASSWORD_NEEDED})


class FlowRateLimited(Exception):
    pass


async def create_flow(
    session: AsyncSession, hasher: Hasher, user_id: int, now: datetime, ttl_seconds: int, max_per_hour: int
) -> tuple[AuthenticationFlow, str]:
    recent = await session.scalar(
        select(func.count())
        .select_from(AuthenticationFlow)
        .where(
            AuthenticationFlow.user_id == user_id, AuthenticationFlow.created_at > now - timedelta(hours=1)
        )
    )
    if int(recent or 0) >= max_per_hour:
        raise FlowRateLimited()
    # Only one live flow per user: older open flows are invalidated.
    old = await session.scalars(
        select(AuthenticationFlow).where(
            AuthenticationFlow.user_id == user_id, AuthenticationFlow.state.in_(OPEN_STATES)
        )
    )
    for f in old.all():
        f.state = FlowState.EXPIRED
        f.failure_code = "superseded"
    token = new_token(32)
    flow = AuthenticationFlow(
        user_id=user_id,
        token_hash=hasher.hash("flow-token", token),
        state=FlowState.CREATED,
        expires_at=now + timedelta(seconds=ttl_seconds),
        created_at=now,
    )
    session.add(flow)
    await session.flush()
    return flow, token


async def open_flow(
    session: AsyncSession, hasher: Hasher, token: str, now: datetime
) -> tuple[AuthenticationFlow, str] | None:
    """Exchange a one-time link token for a browser secret. Returns None if invalid."""
    if not token or len(token) > 128:
        return None
    flow = await session.scalar(
        select(AuthenticationFlow)
        .where(AuthenticationFlow.token_hash == hasher.hash("flow-token", token))
        .with_for_update()
    )
    if flow is None:
        return None
    if flow.state != FlowState.CREATED:
        return None  # single use
    if flow.expires_at <= now:
        flow.state = FlowState.EXPIRED
        return None
    browser_secret = new_token(32)
    flow.browser_hash = hasher.hash("flow-browser", browser_secret)
    flow.state = FlowState.OPENED
    return flow, browser_secret


async def flow_for_browser(
    session: AsyncSession, hasher: Hasher, browser_secret: str | None, now: datetime
) -> AuthenticationFlow | None:
    if not browser_secret or len(browser_secret) > 128:
        return None
    flow = await session.scalar(
        select(AuthenticationFlow)
        .where(AuthenticationFlow.browser_hash == hasher.hash("flow-browser", browser_secret))
        .with_for_update()
    )
    if flow is None:
        return None
    if flow.state in OPEN_STATES and flow.expires_at <= now:
        flow.state = FlowState.EXPIRED
        flow.failure_code = "expired"
    return flow


def csrf_for(hasher: Hasher, browser_secret: str) -> str:
    return hasher.hash("flow-csrf", browser_secret)


def verify_csrf(hasher: Hasher, browser_secret: str, submitted: str | None) -> bool:
    return bool(submitted) and hasher.verify("flow-csrf", browser_secret, submitted or "")
