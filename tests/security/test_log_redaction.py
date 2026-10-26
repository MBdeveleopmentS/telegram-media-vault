import json
import logging

from vault.logging import REDACTED, get_logger, redact_value

BOT_TOKEN = "7000000001:AAH-test-token-not-real-0123456789abcdefg"
SESSION = "1" + "BQ" * 180 + "=="
API_HASH = "0123456789abcdef0123456789abcdef"


def test_value_patterns():
    for secret in (BOT_TOKEN, SESSION, API_HASH, "+972 50-123-4567", "972501234567"):
        assert secret not in redact_value(f"prefix {secret} suffix")


def test_structlog_redacts_sensitive_keys_and_values(capsys):
    log = get_logger("t")
    log.info(
        "login_attempt",
        password="hunter2",
        code="12345",
        phone="+972501234567",
        session_string=SESSION,
        note=f"token is {BOT_TOKEN}",
        job_id=7,
        user_id=42,
    )
    line = capsys.readouterr().out.strip().splitlines()[-1]
    data = json.loads(line)
    for secret in ("hunter2", "12345", "+972501234567", SESSION, BOT_TOKEN):
        assert secret not in line
    assert data["password"] == REDACTED and data["job_id"] == 7 and data["user_id"] == 42


def test_stdlib_third_party_logs_redacted(capsys):
    logging.getLogger("telethon.network").warning("connecting with %s", BOT_TOKEN)
    assert BOT_TOKEN not in capsys.readouterr().out
