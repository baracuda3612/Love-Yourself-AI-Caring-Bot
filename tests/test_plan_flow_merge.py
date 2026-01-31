import os
import pathlib
import sys

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test-user:test-pass@localhost:5432/test-db",
)
os.environ.setdefault("OPENAI_API_KEY", "test-key")

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app import orchestrator


def test_sanitize_plan_updates_ignores_nulls():
    updates = {
        "duration": None,
        "focus": None,
        "load": None,
        "preferred_time_slots": ["EVENING"],
    }

    cleaned = orchestrator._sanitize_plan_updates(updates)

    assert cleaned == {"preferred_time_slots": ["EVENING"]}


def test_sanitize_plan_updates_rejects_empty_slots():
    updates = {"preferred_time_slots": []}

    cleaned = orchestrator._sanitize_plan_updates(updates)

    assert cleaned == {}


def test_slots_do_not_wipe_base_parameters():
    persistent = {"duration": "SHORT", "focus": "REST", "load": "LITE"}
    updates = {
        "duration": None,
        "focus": None,
        "load": None,
        "preferred_time_slots": ["MORNING", "EVENING"],
    }

    cleaned = orchestrator._sanitize_plan_updates(updates)
    merged = dict(persistent)
    if cleaned:
        merged.update(cleaned)

    assert merged == {
        "duration": "SHORT",
        "focus": "REST",
        "load": "LITE",
        "preferred_time_slots": ["MORNING", "EVENING"],
    }
