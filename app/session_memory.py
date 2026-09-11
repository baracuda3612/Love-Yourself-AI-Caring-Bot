"""Lightweight session memory backed by Redis."""

from __future__ import annotations

import json
import logging
from typing import List, MutableSequence

from redis.asyncio import Redis

from app.redis_client import create_redis_client

logger = logging.getLogger(__name__)


class SessionMemory:
    """Stores short conversation snippets for a user."""

    def __init__(self, redis_client: Redis | None = None, limit: int = 20) -> None:
        self.redis = redis_client or create_redis_client()
        self.limit = limit

    def _messages_key(self, user_id: int) -> str:
        return f"session:{user_id}:messages"

    def _pending_action_key(self, user_id: int) -> str:
        return f"session:{user_id}:pending_action"

    async def set_pending_action(self, user_id: int | None, action: str, ttl_seconds: int = 3600) -> None:
        """Store a short-lived pending action (e.g. collect_evening_time_for_medium)."""
        if user_id is None or self.redis is None:
            return
        try:
            await self.redis.set(self._pending_action_key(user_id), action, ex=ttl_seconds)
        except Exception:
            logger.warning("Failed to set pending_action for user %s", user_id, exc_info=True)

    async def get_pending_action(self, user_id: int | None) -> str | None:
        """Return the current pending action or None if absent/expired."""
        if user_id is None or self.redis is None:
            return None
        try:
            raw = await self.redis.get(self._pending_action_key(user_id))
            if raw is None:
                return None
            return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        except Exception:
            logger.warning("Failed to get pending_action for user %s", user_id, exc_info=True)
            return None

    async def clear_pending_action(self, user_id: int | None) -> None:
        """Clear the pending action."""
        if user_id is None or self.redis is None:
            return
        try:
            await self.redis.delete(self._pending_action_key(user_id))
        except Exception:
            logger.warning("Failed to clear pending_action for user %s", user_id, exc_info=True)

    async def append_message(self, user_id: int | None, role: str, text: str) -> None:
        """Append a message to the tail of the user's history."""

        if user_id is None or self.redis is None:
            return

        try:
            payload = json.dumps({"role": role, "text": text})
            key = self._messages_key(user_id)
            await self.redis.rpush(key, payload)
            await self.redis.ltrim(key, -self.limit, -1)
        except Exception:  # pragma: no cover - defensive
            logger.warning("Failed to append message to Redis", exc_info=True)

    async def get_recent_messages(self, user_id: int | None) -> list[dict]:
        """Return up to ``limit`` most recent messages for the user."""

        if user_id is None or self.redis is None:
            return []

        try:
            raw_messages: MutableSequence[str] = await self.redis.lrange(
                self._messages_key(user_id), -self.limit, -1
            )
        except Exception:  # pragma: no cover - defensive
            logger.warning("Failed to fetch messages from Redis", exc_info=True)
            return []

        messages: List[dict] = []
        for raw in raw_messages:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "ignore")
            try:
                parsed = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue

            if not isinstance(parsed, dict):
                continue
            role = parsed.get("role")
            text = parsed.get("text")
            if isinstance(role, str) and isinstance(text, str):
                messages.append({"role": role, "text": text})

        return messages[-self.limit :]

    async def get_last_bot_message(self, user_id: int | None) -> str | None:
        """Return the last bot message if available."""

        recent = await self.get_recent_messages(user_id)
        for msg in reversed(recent):
            if msg.get("role") == "assistant":
                text = msg.get("text")
                if isinstance(text, str):
                    return text
        return None
