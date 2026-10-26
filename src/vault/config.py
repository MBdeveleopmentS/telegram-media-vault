"""Environment-driven configuration.

Every process loads the same Settings model but validates only the secrets it
actually needs (see ``Settings.require_for``). This keeps least privilege
practical: the bot container never receives the session encryption keys, the
auth-web container never receives the bot's payment-processing duties, etc.
"""

from __future__ import annotations

import base64
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIB = 1024 * 1024


class Component(StrEnum):
    BOT = "bot"
    AUTHWEB = "authweb"
    ENGINE = "engine"
    SCHEDULER = "scheduler"
    MIGRATE = "migrate"
    CLI = "cli"


class ConfigError(RuntimeError):
    """Raised when the environment is unsafe or incomplete for a component."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    environment: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"

    # --- Telegram application credentials (developer-owned, never user-supplied)
    bot_token: SecretStr | None = None
    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr | None = None

    # --- infrastructure
    database_url: str = "postgresql+asyncpg://vault:vault@localhost:5432/vault"
    redis_url: str = "redis://localhost:6379/0"
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # --- secrets
    # Comma separated "key_id:base64(32 bytes)" pairs. Only auth-web (encrypt) and
    # engine (decrypt) receive this variable.
    session_encryption_keys: SecretStr | None = None
    session_encryption_active_key_id: str | None = None
    # HMAC key for hashing flow tokens, phone numbers (rate limiting) and IPs.
    app_secret_key: SecretStr | None = None

    # --- auth web
    public_base_url: str = "http://localhost:8085"
    authweb_bind_host: str = "127.0.0.1"
    authweb_port: int = 8085
    auth_flow_ttl_seconds: int = 600
    auth_max_code_attempts: int = 5
    auth_max_password_attempts: int = 3
    auth_max_flows_per_user_per_hour: int = 5
    auth_max_requests_per_ip_per_minute: int = 30
    trust_proxy_headers: bool = False

    # --- bot
    bot_mode: Literal["polling", "webhook"] = "polling"
    webhook_secret: SecretStr | None = None
    webhook_path: str = "/tg/webhook"
    support_contact: str = "@your_support_handle"
    source_code_url: str = "https://github.com/mosheisraelov01-gif/telegram-media-vault"

    # --- plans (server-side source of truth; change here, never in handlers)
    free_max_chats: int = 3
    free_max_file_mib: int = 5
    premium_max_chats: int = 10
    premium_max_file_mib: int = 15
    premium_price_stars: int = 150
    premium_period_days: int = 90
    invoice_ttl_hours: int = 24

    # --- engine / queue
    engine_shard: int = 0
    engine_shards: int = 1
    worker_concurrency: int = 4
    per_user_concurrency: int = 2
    premium_weight: int = 3
    free_weight: int = 1
    job_max_attempts: int = 6
    job_lease_seconds: int = 120
    retry_base_seconds: int = 5
    retry_max_seconds: int = 900
    max_pending_jobs_per_user: int = 300
    temp_dir: str = "/var/lib/vault/tmp"
    temp_max_age_seconds: int = 3600
    catchup_max_messages: int = 100
    reconcile_interval_seconds: int = 15
    # Default deployment: engine also hosts in-chat login and the scheduler (2 processes total).
    engine_hosts_login: bool = True
    engine_hosts_scheduler: bool = True

    # --- notifications
    issue_digest_interval_seconds: int = 900

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.environment == "production":
            if not self.public_base_url.startswith("https://"):
                raise ConfigError("PUBLIC_BASE_URL must use https:// in production")
            if self.log_level.upper() == "DEBUG":
                raise ConfigError("DEBUG logging is not allowed in production (risk of secret leakage)")
            if self.bot_mode == "webhook" and not self.webhook_secret:
                raise ConfigError("WEBHOOK_SECRET is required for webhook mode in production")
        if self.engine_shards < 1 or not 0 <= self.engine_shard < self.engine_shards:
            raise ConfigError("ENGINE_SHARD must be in [0, ENGINE_SHARDS)")
        if self.premium_weight < 1 or self.free_weight < 1:
            raise ConfigError("queue weights must be >= 1 (free users must never starve)")
        return self

    # ------------------------------------------------------------------ helpers
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def encryption_keys(self) -> dict[str, bytes]:
        if self.session_encryption_keys is None:
            raise ConfigError("SESSION_ENCRYPTION_KEYS is not configured for this component")
        keys: dict[str, bytes] = {}
        for item in self.session_encryption_keys.get_secret_value().split(","):
            item = item.strip()
            if not item:
                continue
            kid, _, b64 = item.partition(":")
            try:
                raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
            except ValueError as exc:  # pragma: no cover - defensive
                raise ConfigError(f"encryption key {kid!r} is not valid base64") from exc
            if len(raw) != 32:
                raise ConfigError(f"encryption key {kid!r} must be exactly 32 bytes")
            keys[kid] = raw
        if not keys:
            raise ConfigError("SESSION_ENCRYPTION_KEYS is empty")
        return keys

    def active_key_id(self) -> str:
        keys = self.encryption_keys()
        kid = self.session_encryption_active_key_id or next(iter(keys))
        if kid not in keys:
            raise ConfigError("SESSION_ENCRYPTION_ACTIVE_KEY_ID does not match any configured key")
        return kid

    def require_for(self, component: Component) -> None:
        """Fail fast if the component lacks a required setting."""
        missing: list[str] = []

        def need(name: str) -> None:
            if getattr(self, name) in (None, ""):
                missing.append(name.upper())

        if component in (Component.BOT, Component.ENGINE, Component.SCHEDULER, Component.AUTHWEB):
            need("bot_token")
        if component in (Component.AUTHWEB, Component.ENGINE):
            need("telegram_api_id")
            need("telegram_api_hash")
            need("session_encryption_keys")
        if component in (Component.AUTHWEB, Component.BOT) or (
            component == Component.ENGINE and self.engine_hosts_login
        ):
            need("app_secret_key")
        if missing:
            raise ConfigError(f"missing required settings for {component}: {', '.join(sorted(missing))}")
        if component in (Component.AUTHWEB, Component.ENGINE):
            self.active_key_id()  # validates key material
        if self.app_secret_key is not None and len(self.app_secret_key.get_secret_value()) < 32:
            raise ConfigError("APP_SECRET_KEY must be at least 32 characters")

    # Plan-related conveniences
    @property
    def free_max_file_bytes(self) -> int:
        return self.free_max_file_mib * MIB

    @property
    def premium_max_file_bytes(self) -> int:
        return self.premium_max_file_mib * MIB


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def override_settings(settings: Settings) -> None:
    """Test hook: replace the cached settings object."""
    get_settings.cache_clear()
    _OVERRIDE.clear()
    _OVERRIDE.append(settings)


_OVERRIDE: list[Settings] = []


def settings() -> Settings:
    return _OVERRIDE[0] if _OVERRIDE else get_settings()


__all__ = ["MIB", "Component", "ConfigError", "Field", "Settings", "get_settings", "settings"]
