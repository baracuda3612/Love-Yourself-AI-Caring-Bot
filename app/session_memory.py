"""PostgreSQL conversation authority with a bounded disposable Redis snapshot."""
from __future__ import annotations

import asyncio
import json
import logging

from app.access import environment
from app.redis_client import create_redis_client, create_sync_redis_client

logger = logging.getLogger(__name__)


class SessionMemory:
    def __init__(self, redis_client=None, limit=20, session_factory=None, cache_client=None):
        self.redis = redis_client if redis_client is not None else create_redis_client()
        self.limit = min(max(1, limit), 20)
        self.session_factory = session_factory
        self.cache = cache_client if cache_client is not None else create_sync_redis_client()

    def _messages_key(self, user_id):
        return f"ly:{environment()}:session:{user_id}:messages:v1"

    def _pending_action_key(self, user_id):
        return f"ly:{environment()}:session:{user_id}:pending_action:v1"

    async def set_pending_action(self, user_id, action, ttl_seconds=3600):
        if user_id is None or self.cache is None:
            return
        def write():
            from app.db import SessionLocal, User
            try:
                with (self.session_factory or SessionLocal)() as db:
                    user = db.query(User).filter_by(id=user_id).with_for_update().first()
                    if user and user.is_active:
                        self.cache.set(self._pending_action_key(user_id), action, ex=max(1,min(ttl_seconds,3600)))
            except Exception:
                logger.warning('pending_action_write_failed')
        await asyncio.to_thread(write)

    async def get_pending_action(self, user_id):
        if user_id is not None and self.redis is not None:
            try:
                raw = await self.redis.get(self._pending_action_key(user_id))
                return raw.decode() if isinstance(raw,bytes) else raw
            except Exception:
                logger.warning('pending_action_read_failed')
        return None

    async def clear_pending_action(self, user_id):
        if user_id is not None and self.redis is not None:
            try:
                await self.redis.delete(self._pending_action_key(user_id))
            except Exception:
                logger.warning('pending_action_delete_failed')

    async def append_message(self, user_id, role, text):
        # Callers persist ChatHistory; never create a second conversation writer.
        if user_id is not None and self.redis is not None:
            try:
                await self.redis.delete(self._messages_key(user_id), f'session:{user_id}:messages')
            except Exception:
                logger.warning('conversation_cache_invalidation_failed')

    async def get_recent_messages(self, user_id):
        if user_id is None:
            return []
        from app.db import SessionLocal
        from app.privacy import retained_messages
        def read_and_rebuild():
            from app.db import User
            from app.privacy import utcnow
            from datetime import datetime
            with (self.session_factory or SessionLocal)() as db:
                # All blocking DB/cache work stays in one worker. Holding the
                # user lock prevents a late cache write after erasure, without
                # blocking the bot event loop while awaiting Redis.
                user = db.query(User).filter_by(id=user_id).with_for_update().first()
                messages = retained_messages(db, user_id, self.limit) if user and user.is_active else []
                if self.cache is not None:
                    try:
                        ttl = min([86400] + [int((datetime.fromisoformat(m['expires_at'])-utcnow()).total_seconds()) for m in messages])
                        if messages and ttl > 0:
                            self.cache.set(self._messages_key(user_id), json.dumps(messages), ex=ttl)
                        else:
                            self.cache.delete(self._messages_key(user_id))
                        self.cache.delete(f'session:{user_id}:messages')
                    except Exception:
                        logger.warning('conversation_cache_rebuild_failed')
                return messages
        return await asyncio.to_thread(read_and_rebuild)

    async def get_last_bot_message(self, user_id):
        for msg in reversed(await self.get_recent_messages(user_id)):
            if msg['role'] == 'assistant':
                return msg['text']
        return None

    async def delete_user(self, user_id):
        if self.redis is None:
            # No cache configured means no external cache to erase. A configured
            # unavailable Redis raises, leaving the durable deletion pending.
            from app.config import settings
            if settings.REDIS_URL:
                raise RuntimeError('redis_erasure_unavailable')
            return
        await self.redis.delete(self._messages_key(user_id), self._pending_action_key(user_id),
            f'session:{user_id}:messages', f'session:{user_id}:pending_action')
