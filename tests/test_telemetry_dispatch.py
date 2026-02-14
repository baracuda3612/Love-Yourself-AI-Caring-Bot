import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test-user:test-pass@localhost:5432/test-db")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app import telemetry


def test_dispatch_system_message_skips_when_tg_id_missing(caplog):
    user = SimpleNamespace(id=42, tg_id=None)

    telemetry._dispatch_system_message(user, "hello")

    assert "has no tg_id" in caplog.text


def test_dispatch_system_message_uses_asyncio_run_when_no_running_loop(monkeypatch):
    user = SimpleNamespace(id=99, tg_id=123456)
    called = {"run": 0}

    def fake_get_running_loop():
        raise RuntimeError("no running loop")

    def fake_asyncio_run(coro):
        called["run"] += 1
        close = getattr(coro, "close", None)
        if close is not None:
            close()

    monkeypatch.setattr(telemetry.asyncio, "get_running_loop", fake_get_running_loop)
    monkeypatch.setattr(telemetry.asyncio, "run", fake_asyncio_run)

    telemetry._dispatch_system_message(user, "hello")

    assert called["run"] == 1
