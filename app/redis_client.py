"""Redis client and storage factories."""

from __future__ import annotations

import logging
from functools import lru_cache

import redis.asyncio as redis
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage

from app.config import settings

logger = logging.getLogger(__name__)


@lru_cache
def create_redis_client(url: str | None = None) -> redis.Redis | None:
    """Create (and cache) a Redis client.

    Returns ``None`` if URL is missing or client cannot be created.
    """

    redis_url = url or settings.REDIS_URL
    if not redis_url:
        logger.warning("Redis URL is not configured; Redis features are disabled")
        return None

    try:
        return redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to create Redis client: %s", exc)
        return None


def create_fsm_storage(redis_client: redis.Redis | None = None) -> RedisStorage | None:
    """Create Redis storage for FSM if possible."""

    client = redis_client or create_redis_client()
    if client is None:
        return None

    return RedisStorage(redis=client, key_builder=DefaultKeyBuilder(with_bot_id=True))



# Shared app-wide Redis client instance
redis_client = create_redis_client()
