"""Structured JSON logging with mandatory secret redaction.

Redaction happens in two layers:
1. Keys whose names look sensitive are replaced wholesale.
2. String values are scanned for token-like patterns (bot tokens, long hex/base64
   blobs that could be session strings) and masked.

Never rely on redaction as the primary control: code must not pass secrets to
the logger in the first place. This is defense in depth.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

SENSITIVE_KEY_RE = re.compile(
    r"(token|password|passwd|secret|session|auth_key|api_hash|phone|code|hash|cookie|authorization|"
    r"csrf|key|charge_id|caption|title|username|first_name|last_name)",
    re.IGNORECASE,
)
# Keys that match the pattern above but are known-safe identifiers.
SAFE_KEYS = frozenset(
    {
        "job_id",
        "user_id",
        "account_id",
        "chat_ref",
        "event",
        "error_code",
        "skip_reason",
        "state",
        "status_code",
        "key_id",
        "media_kind",
        "attempt",
        "queue",
        "shard",
        "component",
        "level",
        "timestamp",
        "logger",
        "payment_id",
        "flow_id",
    }
)
REDACTED = "[REDACTED]"

_VALUE_PATTERNS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # bot tokens
    re.compile(r"\b1[A-Za-z0-9_-]{200,}={0,2}"),  # Telethon StringSession (starts with version '1')
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),  # api_hash and other hex secrets
    re.compile(r"\+\d[\d \-]{6,}\d|\b\d{9,15}\b"),  # phone-number-like digit runs
]


def redact_value(value: str) -> str:
    for pattern in _VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def _redact(obj: Any, depth: int = 0) -> Any:
    if depth > 5:
        return obj
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if ks in SAFE_KEYS:
                out[ks] = v
            elif SENSITIVE_KEY_RE.search(ks):
                out[ks] = REDACTED
            else:
                out[ks] = _redact(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return type(obj)(_redact(v, depth + 1) for v in obj)
    if isinstance(obj, str):
        return redact_value(obj)
    return obj


def redaction_processor(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = _redact(dict(event_dict))
    return redacted


class _StdlibRedactingFilter(logging.Filter):
    """Applies value redaction to third-party (stdlib) log records, e.g. Telethon/aiogram."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            return True
        record.msg = redact_value(msg)
        record.args = None
        return True


class _StdoutLogger:
    """Writes to whatever ``sys.stdout`` currently is (robust to test capture swaps)."""

    def msg(self, message: str) -> None:
        print(message, file=sys.stdout, flush=True)

    log = debug = info = warning = warn = error = critical = exception = fatal = msg


def configure_logging(component: str, level: str = "INFO") -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_StdlibRedactingFilter())
    handler.setFormatter(
        logging.Formatter('{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(lvl)
    # Third-party libraries can be chatty and occasionally include request details.
    for noisy in ("telethon", "aiogram", "aiohttp", "httpx", "asyncio", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))
    # Uvicorn access logs would include the one-time auth URL path; disable entirely.
    logging.getLogger("uvicorn.access").disabled = True

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redaction_processor,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=lambda *_: _StdoutLogger(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(component=component)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
