"""Deterministic, non-production configuration for test collection."""

from __future__ import annotations

import os

import pytest


_TEST_ENV = {
    "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
    "DATABASE_URL": (
        "postgresql+psycopg2://love_yourself_test:love_yourself_test@"
        "127.0.0.1:55432/love_yourself_test"
    ),
    "OPENAI_API_KEY": "test-key",
    "REDIS_URL": "redis://127.0.0.1:56379/0",
    "REPORT_TOKEN_SECRET": "test-report-secret",
    "ENVIRONMENT": "test",
}

for _name, _value in _TEST_ENV.items():
    os.environ.setdefault(_name, _value)


@pytest.fixture
def anyio_backend() -> str:
    """The product runtime is asyncio-only; do not synthesize Trio variants."""

    return "asyncio"
