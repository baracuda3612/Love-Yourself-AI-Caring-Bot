"""Typed, fail-closed application configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Set
from urllib.parse import urlparse

from dotenv import load_dotenv


class Environment(StrEnum):
    """Supported deployment environments."""

    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class ConfigurationError(RuntimeError):
    """Raised for invalid configuration without disclosing rejected values."""


_PLACEHOLDER_MARKERS = (
    "change-me",
    "changeme",
    "example",
    "placeholder",
    "replace-me",
    "replace_me",
    "your-",
    "your_",
)
_BOT_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return not normalized or any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def _required(source: Mapping[str, str], name: str, errors: list[str]) -> str:
    value = source.get(name, "").strip()
    if not value:
        errors.append(f"{name} is required")
    return value


def _integer(
    source: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    errors: list[str],
) -> int:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be an integer")
        return default
    if not minimum <= value <= maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}")
    return value


def _floating(
    source: Mapping[str, str],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
    errors: list[str],
) -> float:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"{name} must be a number")
        return default
    if not minimum <= value <= maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}")
    return value


def _admin_ids(raw: str | None, errors: list[str]) -> Set[int]:
    if raw is None or not raw.strip():
        return set()
    result: Set[int] = set()
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            admin_id = int(candidate)
        except ValueError:
            errors.append("ADMIN_IDS must be a comma-separated list of integers")
            return set()
        if admin_id <= 0:
            errors.append("ADMIN_IDS entries must be positive integers")
            return set()
        result.add(admin_id)
    return result


def _url(
    name: str,
    value: str,
    schemes: set[str],
    errors: list[str],
    *,
    require_https: bool = False,
) -> None:
    if not value:
        return
    parsed = urlparse(value)
    if parsed.scheme not in schemes or not parsed.hostname:
        errors.append(f"{name} must be a valid {', '.join(sorted(schemes))} URL")
    elif require_https and parsed.scheme != "https":
        errors.append(f"{name} must use HTTPS outside development")


@dataclass
class Settings:
    """Validated environment-driven settings used by the runtime."""

    BOT_TOKEN: str
    DATABASE_URL: str
    OPENAI_API_KEY: str
    ENVIRONMENT: str
    DEPLOYMENT_ID: str

    ADMIN_IDS: Set[int] = field(default_factory=set)
    TZ: str = "Europe/Kyiv"
    MODEL: str = "gpt-5-mini"
    PLAN_MODEL: str = "gpt-4.1-mini"
    COACH_MODEL: str = "gpt-5.1"
    MAX_TOKENS: int = 300
    TEMPERATURE: float = 0.7
    REDIS_URL: str = ""
    REPORT_TOKEN_SECRET: str = "local-development-only-secret"
    APP_BASE_URL: str = "http://127.0.0.1:8000"
    BOT_USERNAME: str = "local_dev_bot"
    PORT: int = 8000
    OPENAI_PROJECT_ID: str = ""
    ACCESS_TOKEN_SECRET: str = "local-access-secret-not-for-deployment"
    ACCESS_KEY_VERSION: int = 1
    ACCESS_OPERATOR_SECRET: str = ""
    ACCESS_ISOLATION_INVENTORY: str = ""
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    MICROSOFT_CLIENT_ID: str = ""
    MICROSOFT_CLIENT_SECRET: str = ""
    SMTP_HOST: str = ""
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_PORT: int = 587

    IS_DEV: bool = True
    IS_STAGING: bool = False
    IS_PROD: bool = False

    @classmethod
    def from_mapping(cls, source: Mapping[str, str]) -> "Settings":
        """Build settings without exposing rejected values in errors or logs."""

        errors: list[str] = []
        environment_raw = _required(source, "ENVIRONMENT", errors).lower()
        try:
            environment = Environment(environment_raw)
        except ValueError:
            errors.append("ENVIRONMENT must be one of: dev, staging, prod")
            environment = Environment.DEV

        bot_token = _required(source, "BOT_TOKEN", errors)
        database_url = _required(source, "DATABASE_URL", errors)
        openai_api_key = _required(source, "OPENAI_API_KEY", errors)

        deployment_id = source.get("DEPLOYMENT_ID", "").strip()
        redis_url = source.get("REDIS_URL", "").strip()
        report_secret = source.get(
            "REPORT_TOKEN_SECRET", "local-development-only-secret"
        ).strip()
        app_base_url = source.get("APP_BASE_URL", "http://127.0.0.1:8000").strip()
        bot_username = source.get("BOT_USERNAME", "local_dev_bot").strip().lstrip("@")

        if any(character.isspace() for character in bot_token):
            errors.append("BOT_TOKEN must not contain whitespace")

        _url(
            "DATABASE_URL",
            database_url,
            {"postgresql", "postgresql+psycopg2"},
            errors,
        )
        if redis_url:
            _url("REDIS_URL", redis_url, {"redis", "rediss"}, errors)

        if environment is not Environment.DEV:
            if not deployment_id:
                errors.append("DEPLOYMENT_ID is required outside development")
            if not redis_url:
                errors.append("REDIS_URL is required outside development")
            if not source.get("APP_BASE_URL", "").strip():
                errors.append("APP_BASE_URL is required outside development")
            if not source.get("BOT_USERNAME", "").strip():
                errors.append("BOT_USERNAME is required outside development")
            if _is_placeholder(bot_token):
                errors.append("BOT_TOKEN must not be a placeholder outside development")
            elif not _BOT_TOKEN_RE.fullmatch(bot_token):
                errors.append("BOT_TOKEN must have Telegram token format outside development")
            if _is_placeholder(openai_api_key):
                errors.append("OPENAI_API_KEY must not be a placeholder outside development")
            if _is_placeholder(report_secret) or len(report_secret) < 32:
                errors.append(
                    "REPORT_TOKEN_SECRET must be a non-placeholder value of at least 32 characters outside development"
                )
            if _is_placeholder(app_base_url):
                errors.append("APP_BASE_URL must not be a placeholder outside development")
            if _is_placeholder(bot_username):
                errors.append("BOT_USERNAME must not be a placeholder outside development")
            _url("APP_BASE_URL", app_base_url, {"https"}, errors, require_https=True)

        if not _BOT_USERNAME_RE.fullmatch(bot_username):
            errors.append("BOT_USERNAME must contain 5-32 letters, digits, or underscores")

        admin_ids = _admin_ids(source.get("ADMIN_IDS"), errors)
        max_tokens = _integer(source, "MAX_TOKENS", 300, 1, 32768, errors)
        temperature = _floating(source, "TEMPERATURE", 0.7, 0.0, 2.0, errors)
        port = _integer(source, "PORT", 8000, 1, 65535, errors)
        access_secret = source.get("ACCESS_TOKEN_SECRET", "local-access-secret-not-for-deployment").strip()
        operator_secret = source.get("ACCESS_OPERATOR_SECRET", "").strip()
        key_version = _integer(source, "ACCESS_KEY_VERSION", 1, 1, 1000000, errors)
        smtp_port = _integer(source, "SMTP_PORT", 587, 1, 65535, errors)
        if environment is not Environment.DEV:
            if not source.get("OPENAI_PROJECT_ID", "").startswith("proj_"):
                errors.append("OPENAI_PROJECT_ID is required outside development")
            for name, value in (("ACCESS_TOKEN_SECRET", access_secret), ("ACCESS_OPERATOR_SECRET", operator_secret)):
                if len(value) < 32 or _is_placeholder(value) or value.startswith("local-"):
                    errors.append(f"{name} must be a dedicated random secret outside development")
            if access_secret in {report_secret, operator_secret, bot_token, openai_api_key}:
                errors.append("ACCESS_TOKEN_SECRET must be independent")
            if operator_secret in {report_secret, bot_token, openai_api_key}:
                errors.append("ACCESS_OPERATOR_SECRET must be independent")
            if not source.get("ACCESS_ISOLATION_INVENTORY", "").strip():
                errors.append("ACCESS_ISOLATION_INVENTORY is required outside development")
            for provider in ("GOOGLE", "MICROSOFT"):
                if bool(source.get(provider + "_CLIENT_ID")) != bool(source.get(provider + "_CLIENT_SECRET")):
                    errors.append(f"{provider} client credentials must be configured together")
        if environment is Environment.PROD:
            for name in ("SMTP_HOST", "SMTP_FROM", "SMTP_USERNAME", "SMTP_PASSWORD"):
                if not source.get(name, "").strip():
                    errors.append(f"{name} is required for verified-email fallback in production")

        if errors:
            raise ConfigurationError("Invalid configuration: " + "; ".join(errors))

        return cls(
            BOT_TOKEN=bot_token,
            DATABASE_URL=database_url,
            OPENAI_API_KEY=openai_api_key,
            ENVIRONMENT=environment.value,
            DEPLOYMENT_ID=deployment_id or "local-dev",
            ADMIN_IDS=admin_ids,
            TZ=source.get("TZ", "Europe/Kyiv").strip() or "Europe/Kyiv",
            MODEL=source.get("MODEL", "gpt-5-mini").strip() or "gpt-5-mini",
            PLAN_MODEL=source.get("PLAN_MODEL", "gpt-4.1-mini").strip()
            or "gpt-4.1-mini",
            COACH_MODEL=source.get("COACH_MODEL", "gpt-5.1").strip() or "gpt-5.1",
            MAX_TOKENS=max_tokens,
            TEMPERATURE=temperature,
            REDIS_URL=redis_url,
            REPORT_TOKEN_SECRET=report_secret,
            APP_BASE_URL=app_base_url.rstrip("/"),
            BOT_USERNAME=bot_username,
            PORT=port,
            OPENAI_PROJECT_ID=source.get("OPENAI_PROJECT_ID", ""),
            ACCESS_TOKEN_SECRET=access_secret,
            ACCESS_KEY_VERSION=key_version,
            ACCESS_OPERATOR_SECRET=operator_secret,
            ACCESS_ISOLATION_INVENTORY=source.get("ACCESS_ISOLATION_INVENTORY", ""),
            GOOGLE_CLIENT_ID=source.get("GOOGLE_CLIENT_ID", ""),
            GOOGLE_CLIENT_SECRET=source.get("GOOGLE_CLIENT_SECRET", ""),
            MICROSOFT_CLIENT_ID=source.get("MICROSOFT_CLIENT_ID", ""),
            MICROSOFT_CLIENT_SECRET=source.get("MICROSOFT_CLIENT_SECRET", ""),
            SMTP_HOST=source.get("SMTP_HOST", ""), SMTP_PORT=smtp_port,
            SMTP_USERNAME=source.get("SMTP_USERNAME", ""), SMTP_PASSWORD=source.get("SMTP_PASSWORD", ""),
            SMTP_FROM=source.get("SMTP_FROM", ""),
            IS_DEV=environment is Environment.DEV,
            IS_STAGING=environment is Environment.STAGING,
            IS_PROD=environment is Environment.PROD,
        )


load_dotenv()
settings = Settings.from_mapping(os.environ)

# Backwards-compatible aliases for existing imports.
BOT_TOKEN = settings.BOT_TOKEN
OPENAI_API_KEY = settings.OPENAI_API_KEY
ADMIN_IDS = settings.ADMIN_IDS
TZ = settings.TZ
MODEL = settings.MODEL
PLAN_MODEL = settings.PLAN_MODEL
COACH_MODEL = settings.COACH_MODEL
MAX_TOKENS = settings.MAX_TOKENS
TEMPERATURE = settings.TEMPERATURE
DATABASE_URL = settings.DATABASE_URL
ENVIRONMENT = settings.ENVIRONMENT
IS_DEV = settings.IS_DEV
IS_STAGING = settings.IS_STAGING
IS_PROD = settings.IS_PROD
REDIS_URL = settings.REDIS_URL
REPORT_TOKEN_SECRET = settings.REPORT_TOKEN_SECRET
APP_BASE_URL = settings.APP_BASE_URL
BOT_USERNAME = settings.BOT_USERNAME
