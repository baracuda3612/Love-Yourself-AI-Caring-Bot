from __future__ import annotations

import pytest

from app.config import ConfigurationError, Settings


def _base_config(**overrides: str) -> dict[str, str]:
    config = {
        "ENVIRONMENT": "dev",
        "BOT_TOKEN": "local-bot-token",
        "DATABASE_URL": "postgresql+psycopg2://user:pass@127.0.0.1:5432/app",
        "OPENAI_API_KEY": "local-openai-key",
    }
    config.update(overrides)
    return config


def test_dev_configuration_has_deliberate_local_defaults() -> None:
    settings = Settings.from_mapping(_base_config())

    assert settings.ENVIRONMENT == "dev"
    assert settings.DEPLOYMENT_ID == "local-dev"
    assert settings.REDIS_URL == ""
    assert settings.PORT == 8000
    assert settings.IS_DEV is True


@pytest.mark.parametrize("environment", ["", "production", "test", "prd"])
def test_environment_identity_is_explicit_and_closed(environment: str) -> None:
    with pytest.raises(ConfigurationError, match="ENVIRONMENT"):
        Settings.from_mapping(_base_config(ENVIRONMENT=environment))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_TOKENS", "many"),
        ("MAX_TOKENS", "0"),
        ("TEMPERATURE", "hot"),
        ("TEMPERATURE", "2.1"),
        ("PORT", "0"),
        ("PORT", "not-a-port"),
        ("ADMIN_IDS", "123,not-an-id"),
    ],
)
def test_malformed_typed_values_fail_instead_of_falling_back(
    name: str, value: str
) -> None:
    with pytest.raises(ConfigurationError, match=name):
        Settings.from_mapping(_base_config(**{name: value}))


def test_staging_requires_isolated_runtime_dependencies() -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_mapping(_base_config(ENVIRONMENT="staging"))

    message = str(raised.value)
    assert "DEPLOYMENT_ID" in message
    assert "REDIS_URL" in message
    assert "REPORT_TOKEN_SECRET" in message
    assert "APP_BASE_URL" in message
    assert "BOT_USERNAME" in message
    assert "https" in message.lower()


def test_valid_production_configuration_is_accepted() -> None:
    settings = Settings.from_mapping(
        _base_config(
            ENVIRONMENT="prod",
            DEPLOYMENT_ID="love-yourself-production",
            BOT_TOKEN="123456789:a-production-shaped-token-that-is-not-logged",
            OPENAI_API_KEY="sk-production-shaped-key-that-is-not-logged",
            REDIS_URL="rediss://redis.internal:6379/0",
            REPORT_TOKEN_SECRET="a" * 32,
            APP_BASE_URL="https://app.loveyourself.com/",
            BOT_USERNAME="love_yourself_bot",
            PORT="9000",
        )
    )

    assert settings.IS_PROD is True
    assert settings.IS_DEV is False
    assert settings.APP_BASE_URL == "https://app.loveyourself.com"
    assert settings.PORT == 9000


def test_validation_error_does_not_echo_secret_values() -> None:
    secret = "replace_me_sensitive_value"

    with pytest.raises(ConfigurationError) as raised:
        Settings.from_mapping(
            _base_config(
                ENVIRONMENT="prod",
                DEPLOYMENT_ID="production",
                BOT_TOKEN=secret,
                OPENAI_API_KEY=secret,
                REDIS_URL="redis://redis.internal:6379/0",
                REPORT_TOKEN_SECRET=secret,
                APP_BASE_URL="https://love-yourself.example.com",
                BOT_USERNAME="love_yourself_bot",
            )
        )

    assert secret not in str(raised.value)
