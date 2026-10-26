import pytest

from vault.config import Component, ConfigError, Settings


def test_production_requires_https():
    with pytest.raises(ConfigError):
        Settings(environment="production", public_base_url="http://example.com")


def test_production_forbids_debug_logging():
    with pytest.raises(ConfigError):
        Settings(environment="production", public_base_url="https://x.example", log_level="DEBUG")


def test_webhook_requires_secret_in_production():
    with pytest.raises(ConfigError):
        Settings(environment="production", public_base_url="https://x.example", bot_mode="webhook")


def test_free_weight_cannot_be_zero():
    with pytest.raises(ConfigError):
        Settings(free_weight=0)


def test_component_requirements(settings):
    settings.require_for(Component.ENGINE)
    bot_only = Settings(bot_token="1:x", app_secret_key="y" * 40)
    bot_only.require_for(Component.BOT)  # bot never needs session keys
    with pytest.raises(ConfigError):
        bot_only.require_for(Component.ENGINE)


def test_short_app_secret_rejected():
    with pytest.raises(ConfigError):
        Settings(bot_token="1:x", app_secret_key="short").require_for(Component.BOT)


def test_secrets_not_in_repr(settings):
    r = repr(settings)
    assert settings.bot_token.get_secret_value() not in r
    assert settings.telegram_api_hash.get_secret_value() not in r
