"""Auth web flow with a fake MTProto login client (no real Telegram)."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from sqlalchemy import select

from tests.conftest import seed_chat
from tests.fakes import FakeLoginClient
from vault.authweb.app import create_app
from vault.authweb.flows import FlowRateLimited, create_flow
from vault.db.models import (
    AccountStatus,
    AuthenticationFlow,
    ChatStatus,
    EncryptedSession,
    FlowState,
    MonitoredChat,
    SecurityEvent,
    TelegramAccount,
)
from vault.domain.subscriptions import get_or_create_user
from vault.logging import configure_logging
from vault.queue.redis_queue import CONTROL_CHANNEL
from vault.security.crypto import SealedSession, session_aad

USER_TG = 424242
PHONE = "+972501234567"


class Harness:
    def __init__(self, db, redis, settings, cipher, hasher, clock):
        self.db, self.redis, self.settings, self.cipher, self.hasher, self.clock = (
            db,
            redis,
            settings,
            cipher,
            hasher,
            clock,
        )
        self.notifications: list[tuple[int, str, bool]] = []
        self.login = FakeLoginClient(user_id=USER_TG)
        self.clients: list[FakeLoginClient] = []

        async def notifier(chat_id: int, kind: str, menu: bool) -> None:
            self.notifications.append((chat_id, kind, menu))

        def factory() -> FakeLoginClient:
            self.clients.append(self.login)
            return self.login

        self.app = create_app(
            settings=settings,
            db=db,
            redis=redis,
            cipher=cipher,
            hasher=hasher,
            login_factory=factory,
            notifier=notifier,
            clock=clock,
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost:8085"
        )

    async def new_flow(self, tg_user_id: int = USER_TG) -> str:
        async with self.db.transaction() as s:
            user = await get_or_create_user(s, tg_user_id)
            _, token = await create_flow(s, self.hasher, user.id, self.clock(), 600, 5)
        return token


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, html
    return m.group(1)


@pytest.fixture
def h(db, redis, settings, cipher, hasher, clock) -> Harness:
    return Harness(db, redis, settings, cipher, hasher, clock)


async def open_and_get_csrf(c: httpx.AsyncClient, token: str) -> str:
    r = await c.get(f"/auth/{token}")
    assert r.status_code == 303 and r.headers["location"] == "/auth/flow"
    assert (
        "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    )
    r = await c.get("/auth/flow")
    assert r.status_code == 200 and 'dir="rtl"' in r.text
    return csrf_of(r.text)


async def test_full_onboarding_stores_encrypted_session(h: Harness, capsys):
    configure_logging("test")
    pubsub = h.redis.pubsub()
    await pubsub.subscribe(CONTROL_CHANNEL)
    token = await h.new_flow()
    async with h.client() as c:
        csrf = await open_and_get_csrf(c, token)
        r = await c.post("/auth/flow/phone", data={"phone": PHONE, "consent": "yes", "csrf": csrf})
        assert "קוד אימות" in r.text
        r = await c.post("/auth/flow/code", data={"code": "12345", "csrf": csrf})
        assert "החשבון חובר בהצלחה" in r.text
        assert r.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in r.headers["content-security-policy"]

    async with h.db.session() as s:
        acc = await s.scalar(select(TelegramAccount))
        sess = await s.scalar(select(EncryptedSession))
        flow = await s.scalar(select(AuthenticationFlow))
    assert acc is not None and acc.status == AccountStatus.ACTIVE and acc.tg_user_id == USER_TG
    assert sess is not None and b"FAKESESSION" not in sess.ciphertext
    plain = h.cipher.open(
        SealedSession(sess.key_id, sess.nonce, sess.ciphertext), session_aad(acc.id, USER_TG)
    )
    assert plain.startswith("1FAKESESSION")
    assert (
        flow is not None
        and flow.state == FlowState.COMPLETED
        and flow.phone_hash
        and PHONE not in flow.phone_hash
    )
    assert h.notifications == [(USER_TG, "connected", True)]
    assert h.login.closed  # auth key released before the engine uses it
    msg = None
    for _ in range(5):
        msg = msg or await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
    assert msg and json.loads(msg["data"])["type"] == "account_connected"

    out = capsys.readouterr().out
    for secret in ("12345", PHONE, "501234567", "FAKESESSION"):
        assert secret not in out


async def test_two_factor_flow_and_wrong_password(h: Harness):
    h.login = FakeLoginClient(user_id=USER_TG, password="correct horse")
    token = await h.new_flow()
    async with h.client() as c:
        csrf = await open_and_get_csrf(c, token)
        await c.post("/auth/flow/phone", data={"phone": PHONE, "consent": "yes", "csrf": csrf})
        r = await c.post("/auth/flow/code", data={"code": "12345", "csrf": csrf})
        assert "אימות דו־שלבי" in r.text
        r = await c.post("/auth/flow/password", data={"password": "wrong", "csrf": csrf})
        assert "הסיסמה שגויה" in r.text
        r = await c.post("/auth/flow/password", data={"password": "correct horse", "csrf": csrf})
        assert "החשבון חובר בהצלחה" in r.text


async def test_password_attempt_limit(h: Harness):
    h.login = FakeLoginClient(user_id=USER_TG, password="pw")
    token = await h.new_flow()
    async with h.client() as c:
        csrf = await open_and_get_csrf(c, token)
        await c.post("/auth/flow/phone", data={"phone": PHONE, "consent": "yes", "csrf": csrf})
        await c.post("/auth/flow/code", data={"code": "12345", "csrf": csrf})
        for _ in range(h.settings.auth_max_password_attempts):
            await c.post("/auth/flow/password", data={"password": "nope", "csrf": csrf})
        r = await c.post("/auth/flow/password", data={"password": "pw", "csrf": csrf})
        assert r.status_code == 429
    async with h.db.session() as s:
        assert await s.scalar(select(TelegramAccount)) is None


async def test_wrong_code_then_lockout(h: Harness):
    token = await h.new_flow()
    async with h.client() as c:
        csrf = await open_and_get_csrf(c, token)
        await c.post("/auth/flow/phone", data={"phone": PHONE, "consent": "yes", "csrf": csrf})
        r = await c.post("/auth/flow/code", data={"code": "00000", "csrf": csrf})
        assert "הקוד שגוי" in r.text
        for _ in range(h.settings.auth_max_code_attempts):
            r = await c.post("/auth/flow/code", data={"code": "00000", "csrf": csrf})
        assert r.status_code == 429
        r = await c.post("/auth/flow/code", data={"code": "12345", "csrf": csrf})
        assert "החשבון חובר" not in r.text


async def test_expired_code(h: Harness):
    h.login = FakeLoginClient(user_id=USER_TG, code_expired=True)
    token = await h.new_flow()
    async with h.client() as c:
        csrf = await open_and_get_csrf(c, token)
        await c.post("/auth/flow/phone", data={"phone": PHONE, "consent": "yes", "csrf": csrf})
        r = await c.post("/auth/flow/code", data={"code": "12345", "csrf": csrf})
        assert "פג תוקף הקוד" in r.text


async def test_flood_wait_on_send_code(h: Harness):
    from vault.authweb.login import FloodWait

    h.login = FakeLoginClient(user_id=USER_TG, send_error=FloodWait(600))
    token = await h.new_flow()
    async with h.client() as c:
        csrf = await open_and_get_csrf(c, token)
        r = await c.post("/auth/flow/phone", data={"phone": PHONE, "consent": "yes", "csrf": csrf})
        assert "להמתין" in r.text
    assert h.login.closed


async def test_consent_is_required(h: Harness):
    token = await h.new_flow()
    async with h.client() as c:
        csrf = await open_and_get_csrf(c, token)
        r = await c.post("/auth/flow/phone", data={"phone": PHONE, "csrf": csrf})
        assert "יש לאשר" in r.text
    assert h.clients == []  # nothing sent to Telegram


async def test_identity_mismatch_is_rejected_and_logged_out(h: Harness):
    h.login = FakeLoginClient(user_id=999999)  # someone else's account
    token = await h.new_flow()
    async with h.client() as c:
        csrf = await open_and_get_csrf(c, token)
        await c.post("/auth/flow/phone", data={"phone": PHONE, "consent": "yes", "csrf": csrf})
        r = await c.post("/auth/flow/code", data={"code": "12345", "csrf": csrf})
        assert r.status_code == 403
    assert h.login.logged_out
    async with h.db.session() as s:
        assert await s.scalar(select(TelegramAccount)) is None
        assert await s.scalar(select(EncryptedSession)) is None
        ev = (await s.scalars(select(SecurityEvent.event_type))).all()
    assert "identity_mismatch" in ev
    assert h.notifications == [(USER_TG, "identity_mismatch", False)]


async def test_reconnect_restores_paused_chats(h: Harness, cipher):
    from vault.domain.accounts import disconnect_account

    token = await h.new_flow()
    async with h.client() as c:
        csrf = await open_and_get_csrf(c, token)
        await c.post("/auth/flow/phone", data={"phone": PHONE, "consent": "yes", "csrf": csrf})
        await c.post("/auth/flow/code", data={"code": "12345", "csrf": csrf})
    async with h.db.session() as s:
        acc = await s.scalar(select(TelegramAccount))
        from vault.db.models import User

        user = await s.scalar(select(User))
    chat = await seed_chat(h.db, user, acc, -1005)
    async with h.db.transaction() as s:
        await disconnect_account(s, user.id, h.clock())
    async with h.db.session() as s:
        assert (await s.get(MonitoredChat, chat.id)).status == ChatStatus.PAUSED_ACCOUNT  # type: ignore[union-attr]

    h.login = FakeLoginClient(user_id=USER_TG)
    h.clock.advance(minutes=1)
    token = await h.new_flow()
    async with h.client() as c:
        csrf = await open_and_get_csrf(c, token)
        await c.post("/auth/flow/phone", data={"phone": "+972501234568", "consent": "yes", "csrf": csrf})
        r = await c.post("/auth/flow/code", data={"code": "12345", "csrf": csrf})
        assert "החשבון חובר בהצלחה" in r.text
    async with h.db.session() as s:
        assert (await s.get(MonitoredChat, chat.id)).status == ChatStatus.ACTIVE  # type: ignore[union-attr]
        assert (await s.scalar(select(TelegramAccount))).status == AccountStatus.ACTIVE  # type: ignore[union-attr]


# ------------------------------------------------------------------ security
async def test_link_token_is_single_use(h: Harness):
    token = await h.new_flow()
    async with h.client() as c:
        await open_and_get_csrf(c, token)
    async with h.client() as attacker:
        r = await attacker.get(f"/auth/{token}")
        assert r.status_code == 404
        assert "set-cookie" not in r.headers


async def test_invalid_and_expired_tokens(h: Harness):
    async with h.client() as c:
        assert (await c.get("/auth/not-a-real-token")).status_code == 404
        token = await h.new_flow()
        h.clock.advance(seconds=601)
        assert (await c.get(f"/auth/{token}")).status_code == 404
    async with h.db.session() as s:
        assert (await s.scalar(select(AuthenticationFlow))).state == FlowState.EXPIRED  # type: ignore[union-attr]


async def test_flow_expires_mid_way(h: Harness):
    token = await h.new_flow()
    async with h.client() as c:
        csrf = await open_and_get_csrf(c, token)
        h.clock.advance(seconds=601)
        r = await c.post("/auth/flow/phone", data={"phone": PHONE, "consent": "yes", "csrf": csrf})
        assert "פג תוקף" in r.text
    assert h.clients == []


async def test_new_link_supersedes_old(h: Harness):
    old = await h.new_flow()
    await h.new_flow()
    async with h.client() as c:
        assert (await c.get(f"/auth/{old}")).status_code == 404


async def test_csrf_required(h: Harness):
    token = await h.new_flow()
    async with h.client() as c:
        await open_and_get_csrf(c, token)
        r = await c.post("/auth/flow/phone", data={"phone": PHONE, "consent": "yes", "csrf": "forged"})
        assert r.status_code == 403
        r = await c.post("/auth/flow/phone", data={"phone": PHONE, "consent": "yes"})
        assert r.status_code == 403
    assert h.clients == []


async def test_post_without_cookie_rejected(h: Harness):
    token = await h.new_flow()
    async with h.client() as c:
        csrf = await open_and_get_csrf(c, token)
    async with h.client() as other_browser:
        r = await other_browser.post(
            "/auth/flow/phone", data={"phone": PHONE, "consent": "yes", "csrf": csrf}
        )
        assert r.status_code == 404


async def test_flow_creation_rate_limited(h: Harness):
    async with h.db.transaction() as s:
        user = await get_or_create_user(s, 5150)
        for _ in range(5):
            await create_flow(s, h.hasher, user.id, h.clock(), 600, 5)
        with pytest.raises(FlowRateLimited):
            await create_flow(s, h.hasher, user.id, h.clock(), 600, 5)
        h.clock.advance(hours=1, seconds=1)
        await create_flow(s, h.hasher, user.id, h.clock(), 600, 5)


async def test_ip_rate_limit(h: Harness):
    h.settings.auth_max_requests_per_ip_per_minute = 3
    async with h.client() as c:
        codes = [(await c.get(f"/auth/garbage{i}")).status_code for i in range(5)]
    assert codes[-1] == 429


async def test_phone_rate_limit(h: Harness):
    for i in range(4):
        h.login = FakeLoginClient(user_id=USER_TG)
        h.clock.advance(seconds=1)
        token = await h.new_flow()
        async with h.client() as c:
            csrf = await open_and_get_csrf(c, token)
            r = await c.post("/auth/flow/phone", data={"phone": PHONE, "consent": "yes", "csrf": csrf})
    assert "יותר מדי ניסיונות" in r.text and i == 3


async def test_no_debug_or_docs_endpoints(h: Harness):
    async with h.client() as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await c.get(path)).status_code == 404


async def test_flow_token_not_stored_in_plaintext(h: Harness):
    token = await h.new_flow()
    async with h.db.session() as s:
        f = await s.scalar(select(AuthenticationFlow))
    assert f is not None and token not in f.token_hash and len(f.token_hash) == 64


async def test_stale_flow_after_ttl_does_not_open(h: Harness):
    token = await h.new_flow()
    h.clock.advance(minutes=11)
    async with h.client() as c:
        assert (await c.get(f"/auth/{token}")).status_code == 404
