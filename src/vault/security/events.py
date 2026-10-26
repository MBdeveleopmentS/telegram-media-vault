"""Security event recording with an allow-list of detail fields.

Details are filtered against ALLOWED_DETAIL_KEYS so that no code path can
accidentally persist a login code, password, session or phone number.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vault.db.models import SecurityEvent
from vault.logging import get_logger

log = get_logger(__name__)

ALLOWED_DETAIL_KEYS = frozenset(
    {
        "reason",
        "flow_id",
        "account_id",
        "job_id",
        "payment_id",
        "error_code",
        "expected_user",
        "actual_user_matches",
        "attempts",
        "component",
        "chat_ref",
        "amount",
        "currency",
    }
)


def sanitize_details(details: dict[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    clean: dict[str, Any] = {}
    for k, v in details.items():
        if k not in ALLOWED_DETAIL_KEYS:
            continue
        if isinstance(v, (int, bool)) or v is None:
            clean[k] = v
        else:
            clean[k] = str(v)[:128]
    return clean


async def record(
    session: AsyncSession,
    event_type: str,
    *,
    severity: str = "info",
    user_id: int | None = None,
    ip_hash: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    clean = sanitize_details(details)
    session.add(
        SecurityEvent(
            user_id=user_id, event_type=event_type, severity=severity, details=clean, ip_hash=ip_hash
        )
    )
    log.info("security_event", security_event=event_type, severity=severity, user_id=user_id, **clean)
