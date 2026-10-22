"""Redis client factory."""

from __future__ import annotations

import logging
from functools import lru_cache

import redis.asyncio as redis

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
        return redis.from_url(redis_url, encoding="utf-8", decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to create Redis client: %s", exc)
        return None


@lru_cache
def create_sync_redis_client():
    """Bounded cache I/O runs in the same worker as its PostgreSQL user lock."""
    if not settings.REDIS_URL:
        return None
    from redis import Redis
    return Redis.from_url(settings.REDIS_URL, encoding='utf-8', decode_responses=True,
        socket_timeout=2, socket_connect_timeout=2)
