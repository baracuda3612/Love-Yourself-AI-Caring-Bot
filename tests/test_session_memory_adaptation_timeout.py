import os
import pathlib
import sys
from datetime import timezone

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test-user:test-pass@localhost:5432/test-db",
)
os.environ.setdefault("OPENAI_API_KEY", "test-key")

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app.session_memory import SessionMemory


class _FakeRedis:
    def __init__(self):
        self.storage = {}

    async def set(self, key, value, ex=None):
        self.storage[key] = value

    async def get(self, key):
        return self.storage.get(key)

    async def delete(self, key):
        self.storage.pop(key, None)


@pytest.mark.anyio
async def test_set_adaptation_last_active_stores_timezone_aware_iso():
    redis = _FakeRedis()
    memory = SessionMemory(redis_client=redis)

    await memory.set_adaptation_last_active(5)

    raw = redis.storage[memory._adaptation_last_active_key(5)]
    assert "+00:00" in raw


@pytest.mark.anyio
async def test_get_adaptation_last_active_normalizes_naive_timestamp_to_utc():
    redis = _FakeRedis()
    memory = SessionMemory(redis_client=redis)
    redis.storage[memory._adaptation_last_active_key(6)] = "2026-01-01T10:00:00"

    dt = await memory.get_adaptation_last_active(6)

    assert dt is not None
    assert dt.tzinfo == timezone.utc
