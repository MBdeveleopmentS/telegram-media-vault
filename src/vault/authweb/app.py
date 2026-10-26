"""Auth web: the only place where users type their phone number, login code and 2FA password.

Security properties:
- One-time link token -> HttpOnly SameSite=Strict cookie; token removed from URL.
- CSRF token derived from the browser secret (HMAC), checked on every POST.
- Strict CSP, no third-party resources, no-store caching, no-referrer.
- Per-IP, per-flow, per-phone and per-user rate limits (Redis, fail closed).
- Identity binding: the logged-in MTProto user id must equal the bot user id.
- Codes/passwords are passed straight to Telegram and never stored or logged.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware

from vault.authweb import flows
from vault.authweb.login import (
    CodeExpired,
    FloodWait,
    InvalidCode,
    InvalidPassword,
    InvalidPhone,
    LoginClient,
    LoginError,
    PasswordNeeded,
    PendingLogin,
    PendingLogins,
    PhoneBanned,
    SignUpRequired,
)
from vault.config import Settings
from vault.db.models import AuthenticationFlow, FlowState, User, utcnow
from vault.db.session import Database
from vault.domain.accounts import IdentityMismatch, store_connected_account
from vault.logging import get_logger
from vault.queue.redis_queue import CONTROL_CHANNEL
from vault.security import events
from vault.security.crypto import Hasher, SessionCipher
from vault.security.ratelimit import RateLimiter

log = get_logger(__name__)
HERE = Path(__file__).parent
PHONE_RE = re.compile(r"^\+?[0-9]{7,15}$")
CODE_RE = re.compile(r"^[0-9]{4,8}$")

Notifier = Callable[[int, str, bool], Awaitable[None]]  # (chat_id, text, show_menu)


class SecurityHeaders(BaseHTTPMiddleware):
    def __init__(self, app: Any, production: bool) -> None:
        super().__init__(app)
        self.production = production

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if self.production:
            resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return resp


def create_app(
    *,
    settings: Settings,
    db: Database,
    redis: Redis,
    cipher: SessionCipher,
    hasher: Hasher,
    login_factory: Callable[[], LoginClient],
    notifier: Notifier,
    clock: Callable[[], datetime] = utcnow,
    background: list[Callable[[], Coroutine[Any, Any, None]]] | None = None,
) -> FastAPI:
    s = settings
    pending = PendingLogins(s.auth_flow_ttl_seconds)
    limiter = RateLimiter(redis, prefix="rl:auth")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    cookie_name = "__Host-vault_flow" if s.is_production else "vault_flow"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def janitor() -> None:
            while True:
                await asyncio.sleep(30)
                await pending.expire()

        tasks = [asyncio.create_task(janitor())]
        tasks += [asyncio.create_task(fn()) for fn in background or []]
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(SecurityHeaders, production=s.is_production)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.state.pending = pending

    def client_ip(request: Request) -> str:
        if s.trust_proxy_headers:
            fwd = request.headers.get("x-forwarded-for", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def ip_allowed(request: Request) -> bool:
        ip_h = hasher.hash("ip", client_ip(request))
        return await limiter.hit(f"ip:{ip_h}", s.auth_max_requests_per_ip_per_minute, 60)

    def page(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def error_page(request: Request, message_key: str, status: int = 400) -> HTMLResponse:
        return page(request, "error.html", status=status, message_key=message_key)

    async def load_flow(request: Request) -> tuple[AuthenticationFlow | None, str | None]:
        secret = request.cookies.get(cookie_name)
        async with db.transaction() as session:
            flow = await flows.flow_for_browser(session, hasher, secret, clock())
        return flow, secret

    def render_step(
        request: Request, flow: AuthenticationFlow, secret: str, error: str | None = None
    ) -> HTMLResponse:
        csrf = flows.csrf_for(hasher, secret)
        if flow.state == FlowState.OPENED:
            return page(request, "phone.html", csrf=csrf, error=error)
        if flow.state == FlowState.CODE_SENT:
            return page(request, "code.html", csrf=csrf, error=error)
        if flow.state == FlowState.PASSWORD_NEEDED:
            return page(request, "password.html", csrf=csrf, error=error)
        if flow.state == FlowState.COMPLETED:
            return page(request, "done.html")
        return error_page(request, flow.failure_code or "expired")

    # ------------------------------------------------------------------ routes
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/auth/flow")
    async def show_flow(request: Request) -> Response:
        flow, secret = await load_flow(request)
        if flow is None or secret is None:
            return error_page(request, "invalid_link", 404)
        return render_step(request, flow, secret)

    @app.get("/auth/{token}")
    async def open_link(request: Request, token: str) -> Response:
        if not await ip_allowed(request):
            return error_page(request, "rate_limited", 429)
        async with db.transaction() as session:
            res = await flows.open_flow(session, hasher, token, clock())
            if res is None:
                await events.record(
                    session,
                    "auth_link_invalid",
                    severity="warning",
                    ip_hash=hasher.hash("ip", client_ip(request)),
                )
        if res is None:
            return error_page(request, "invalid_link", 404)
        _, browser_secret = res
        resp = RedirectResponse("/auth/flow", status_code=303)
        resp.set_cookie(
            cookie_name,
            browser_secret,
            max_age=s.auth_flow_ttl_seconds,
            httponly=True,
            secure=s.is_production,
            samesite="strict",
            path="/",
        )
        return resp

    async def guarded(
        request: Request, csrf: str | None, expected: FlowState
    ) -> tuple[AuthenticationFlow, str] | Response:
        if not await ip_allowed(request):
            return error_page(request, "rate_limited", 429)
        flow, secret = await load_flow(request)
        if flow is None or secret is None:
            return error_page(request, "invalid_link", 404)
        if not flows.verify_csrf(hasher, secret, csrf):
            async with db.transaction() as session:
                await events.record(
                    session,
                    "auth_csrf_failed",
                    severity="warning",
                    user_id=flow.user_id,
                    details={"flow_id": flow.id},
                )
            return error_page(request, "csrf", 403)
        if flow.state != expected:
            return render_step(request, flow, secret)
        return flow, secret

    async def fail_flow(flow_id: int, code: str) -> None:
        async with db.transaction() as session:
            f = await session.get(AuthenticationFlow, flow_id, with_for_update=True)
            if f is not None:
                f.state = FlowState.FAILED
                f.failure_code = code
        await pending.drop(flow_id)

    @app.post("/auth/flow/phone")
    async def submit_phone(
        request: Request, phone: str = Form(""), consent: str = Form(""), csrf: str = Form("")
    ) -> Response:
        g = await guarded(request, csrf, FlowState.OPENED)
        if isinstance(g, Response):
            return g
        flow, secret = g
        if consent != "yes":
            return render_step(request, flow, secret, error="consent_required")
        normalized = re.sub(r"[\s\-()]", "", phone)
        if not PHONE_RE.match(normalized):
            return render_step(request, flow, secret, error="invalid_phone")
        phone_h = hasher.hash("phone", normalized.lstrip("+"))
        if not await limiter.hit(f"phone:{phone_h}", 3, 3600):
            return render_step(request, flow, secret, error="rate_limited")

        await pending.drop(flow.id)
        client = login_factory()
        try:
            code_hash = await client.send_code(normalized)
        except FloodWait as e:
            await client.close()
            return render_step(request, flow, secret, error=f"flood:{e.seconds}")
        except (InvalidPhone, PhoneBanned) as e:
            await client.close()
            return render_step(request, flow, secret, error=e.code)
        except LoginError:
            await client.close()
            return render_step(request, flow, secret, error="telegram_error")
        pending.put(flow.id, PendingLogin(client=client, phone=normalized, phone_code_hash=code_hash))
        async with db.transaction() as session:
            f = await session.get(AuthenticationFlow, flow.id, with_for_update=True)
            assert f is not None
            f.state = FlowState.CODE_SENT
            f.consent_at = clock()
            f.phone_hash = phone_h
            flow = f
        return render_step(request, flow, secret)

    @app.post("/auth/flow/code")
    async def submit_code(request: Request, code: str = Form(""), csrf: str = Form("")) -> Response:
        g = await guarded(request, csrf, FlowState.CODE_SENT)
        if isinstance(g, Response):
            return g
        flow, secret = g
        p = pending.get(flow.id)
        if p is None:  # auth-web restarted or expired
            await fail_flow(flow.id, "restart_required")
            return error_page(request, "restart_required")
        code = re.sub(r"\D", "", code)
        async with db.transaction() as session:
            f = await session.get(AuthenticationFlow, flow.id, with_for_update=True)
            assert f is not None
            f.code_attempts += 1
            attempts = f.code_attempts
        if attempts > s.auth_max_code_attempts:
            await fail_flow(flow.id, "too_many_attempts")
            return error_page(request, "too_many_attempts", 429)
        if not CODE_RE.match(code):
            return render_step(request, flow, secret, error="invalid_code")
        try:
            tg_user_id = await p.client.sign_in_code(p.phone, code, p.phone_code_hash)
        except PasswordNeeded:
            async with db.transaction() as session:
                f = await session.get(AuthenticationFlow, flow.id, with_for_update=True)
                assert f is not None
                f.state = FlowState.PASSWORD_NEEDED
                flow = f
            return render_step(request, flow, secret)
        except InvalidCode:
            return render_step(request, flow, secret, error="invalid_code")
        except CodeExpired:
            await fail_flow(flow.id, "code_expired")
            return error_page(request, "code_expired")
        except SignUpRequired:
            await fail_flow(flow.id, "signup_required")
            return error_page(request, "signup_required")
        except FloodWait as e:
            return render_step(request, flow, secret, error=f"flood:{e.seconds}")
        except LoginError:
            await fail_flow(flow.id, "telegram_error")
            return error_page(request, "telegram_error")
        return await finalize(request, flow, p, tg_user_id)

    @app.post("/auth/flow/password")
    async def submit_password(request: Request, password: str = Form(""), csrf: str = Form("")) -> Response:
        g = await guarded(request, csrf, FlowState.PASSWORD_NEEDED)
        if isinstance(g, Response):
            return g
        flow, secret = g
        p = pending.get(flow.id)
        if p is None:
            await fail_flow(flow.id, "restart_required")
            return error_page(request, "restart_required")
        async with db.transaction() as session:
            f = await session.get(AuthenticationFlow, flow.id, with_for_update=True)
            assert f is not None
            f.password_attempts += 1
            attempts = f.password_attempts
        if attempts > s.auth_max_password_attempts:
            await fail_flow(flow.id, "too_many_attempts")
            return error_page(request, "too_many_attempts", 429)
        if not password or len(password) > 256:
            return render_step(request, flow, secret, error="invalid_password")
        try:
            tg_user_id = await p.client.sign_in_password(password)
        except InvalidPassword:
            return render_step(request, flow, secret, error="invalid_password")
        except FloodWait as e:
            return render_step(request, flow, secret, error=f"flood:{e.seconds}")
        except LoginError:
            await fail_flow(flow.id, "telegram_error")
            return error_page(request, "telegram_error")
        finally:
            del password
        return await finalize(request, flow, p, tg_user_id)

    async def finalize(
        request: Request, flow: AuthenticationFlow, p: PendingLogin, tg_user_id: int
    ) -> Response:
        now = clock()
        session_string = p.client.session_string()
        async with db.transaction() as session:
            user = await session.get(User, flow.user_id)
            assert user is not None
            f = await session.get(AuthenticationFlow, flow.id, with_for_update=True)
            assert f is not None
            bot_chat_id = user.telegram_user_id
            try:
                acc = await store_connected_account(session, cipher, user, tg_user_id, session_string, now, s)
            except IdentityMismatch:
                acc = None
                f.state = FlowState.FAILED
                f.failure_code = "identity_mismatch"
                await events.record(
                    session,
                    "identity_mismatch",
                    severity="high",
                    user_id=user.id,
                    details={"flow_id": f.id, "actual_user_matches": False},
                )
            else:
                f.state = FlowState.COMPLETED
                f.completed_at = now
                await events.record(
                    session,
                    "account_connected",
                    user_id=user.id,
                    details={"flow_id": f.id, "account_id": acc.id if acc else None},
                )
        del session_string
        if acc is None:
            # Revoke the foreign authorization we just created; never keep it.
            await p.client.log_out()
            await pending.drop(flow.id)
            await notifier(bot_chat_id, "identity_mismatch", False)
            return error_page(request, "identity_mismatch", 403)
        # Release the auth key before the engine connects with it (no concurrent use).
        await pending.drop(flow.id)
        await redis.publish(CONTROL_CHANNEL, json.dumps({"type": "account_connected", "shard": acc.shard}))
        await notifier(bot_chat_id, "connected", True)
        return page(request, "done.html")

    return app
