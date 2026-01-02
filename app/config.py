"""Application configuration management."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Set

from dotenv import load_dotenv

load_dotenv()


def _as_int(value: str | None, default: int) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: str | None, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_admin_ids(raw: str | None) -> Set[int]:
    ids: Set[int] = set()
    if not raw:
        return ids
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids


@dataclass
class Settings:
    """Container for environment-driven application settings."""

    BOT_TOKEN: str = ""
    DATABASE_URL: str = ""
    OPENAI_API_KEY: str = ""
    ADMIN_IDS: Set[int] = field(default_factory=set)
    TZ: str = "Europe/Kyiv"
    MODEL: str = "gpt-4.1"
    COACH_MODEL: str = "gpt-5.1"
    ROUTER_MODEL: str = "gpt-5-mini"
    MAX_TOKENS: int = 300
    TEMPERATURE: float = 0.7
    DEFAULT_DAILY_LIMIT: int = 10
    DEFAULT_SEND_HOUR: int = 9
    REDIS_URL: str = ""
    ENVIRONMENT: str = "dev"
    IS_DEV: bool = True
    IS_STAGING: bool = False
    IS_PROD: bool = False

    def __post_init__(self) -> None:
        bot_token = os.getenv("BOT_TOKEN")
        database_url = os.getenv("DATABASE_URL")
        openai_api_key = os.getenv("OPENAI_API_KEY")

        if not bot_token:
            raise RuntimeError("BOT_TOKEN environment variable is required")
        if not database_url:
            raise RuntimeError("DATABASE_URL environment variable is required")
        if not openai_api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable is required")

        environment = (os.getenv("ENVIRONMENT") or "dev").strip() or "dev"
        environment_normalized = environment.lower()

        self.BOT_TOKEN = bot_token
        self.DATABASE_URL = database_url
        self.OPENAI_API_KEY = openai_api_key
        self.ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS"))
        self.TZ = os.getenv("TZ", "Europe/Kyiv")
        self.MODEL = os.getenv("MODEL", "gpt-4.1")
        self.COACH_MODEL = (
            os.getenv("COACH_MODEL")
            or os.getenv("MODEL_REASONING")
            or os.getenv("REASONING_MODEL")
            or os.getenv("HIGH_REASONING_MODEL")
            or "gpt-4.1"
        )
        self.ROUTER_MODEL = os.getenv("ROUTER_MODEL", "gpt-5-mini")
        self.MAX_TOKENS = _as_int(os.getenv("MAX_TOKENS"), 300)
        self.TEMPERATURE = _as_float(os.getenv("TEMPERATURE"), 0.7)
        self.DEFAULT_DAILY_LIMIT = _as_int(os.getenv("DEFAULT_DAILY_LIMIT"), 10)
        self.DEFAULT_SEND_HOUR = _as_int(os.getenv("DEFAULT_SEND_HOUR"), 9)
        # Empty ``REDIS_URL`` disables Redis-backed features (FSM, session memory).
        self.REDIS_URL = os.getenv("REDIS_URL") or ""
        self.ENVIRONMENT = environment_normalized
        self.IS_DEV = environment_normalized == "dev"
        self.IS_STAGING = environment_normalized == "staging"
        self.IS_PROD = environment_normalized == "prod"


settings = Settings()

# Backwards-compatible aliases for existing imports
BOT_TOKEN = settings.BOT_TOKEN
OPENAI_API_KEY = settings.OPENAI_API_KEY
ADMIN_IDS = settings.ADMIN_IDS
TZ = settings.TZ
MODEL = settings.MODEL
COACH_MODEL = settings.COACH_MODEL
ROUTER_MODEL = settings.ROUTER_MODEL
MAX_TOKENS = settings.MAX_TOKENS
TEMPERATURE = settings.TEMPERATURE
DEFAULT_DAILY_LIMIT = settings.DEFAULT_DAILY_LIMIT
DEFAULT_SEND_HOUR = settings.DEFAULT_SEND_HOUR
DATABASE_URL = settings.DATABASE_URL
ENVIRONMENT = settings.ENVIRONMENT
IS_DEV = settings.IS_DEV
IS_STAGING = settings.IS_STAGING
IS_PROD = settings.IS_PROD
REDIS_URL = settings.REDIS_URL
