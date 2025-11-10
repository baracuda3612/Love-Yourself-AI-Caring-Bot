import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.plan_normalizer import normalize_plan_steps


def test_normalize_steps_prefers_steps_over_entries():
    payload = {
        "steps": [
            {"message": "Step message", "scheduled_for": "10:00"},
        ],
        "entries": [
            {"message": "Entry message", "scheduled_for": "11:00"},
        ],
    }

    normalized = normalize_plan_steps(payload)

    assert len(normalized) == 1
    assert normalized[0]["message"] == "Step message"
    assert normalized[0]["scheduled_for"] == "10:00"


def test_normalize_steps_falls_back_to_entries_and_skips_invalid():
    payload = {
        "entries": [
            {"message": "Entry message", "time": "12:00"},
            "not-a-dict",
            {"scheduled_for": "13:00"},
        ]
    }

    normalized = normalize_plan_steps(payload)

    assert len(normalized) == 1
    assert normalized[0]["message"] == "Entry message"
    assert normalized[0]["scheduled_for"] == "12:00"
