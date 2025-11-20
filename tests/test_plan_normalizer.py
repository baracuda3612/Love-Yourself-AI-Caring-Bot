import sys
from datetime import datetime
from pathlib import Path

import sys
from datetime import datetime
from pathlib import Path

import pytz

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.ai_plans import PLAYBOOKS, _DEFAULT_PLAYBOOK
from app.plan_normalizer import normalize_plan_steps


def test_normalize_full_plan_without_dates():
    payload = {
        "plan_name": "X",
        "steps": [
            {"day": 1, "message": "A"},
            {"day": 2, "message": "B"},
        ],
    }

    steps = normalize_plan_steps(
        payload,
        goal="сон",
        days=7,
        tasks_per_day=1,
        preferred_hour="22:00",
        tz_name="Europe/Kyiv",
    )

    assert len(steps) == 7
    for idx, step in enumerate(steps, start=1):
        assert step["day"] == idx
        assert step["day_index"] == idx - 1
        assert step["slot_index"] == 0
        assert step["status"] == "pending"
        assert step["scheduled_for"] is None
        assert step["job_id"] is None
        assert isinstance(step["proposed_for"], datetime)
        assert step["proposed_for"].tzinfo == pytz.UTC

        local_dt = step["proposed_for"].astimezone(pytz.timezone("Europe/Kyiv"))
        assert local_dt.hour == 22
        assert local_dt.minute == 0


def test_normalize_repeats_short_step_list():
    payload = {
        "steps": [
            {"day": 1, "message": "A"},
        ]
    }

    steps = normalize_plan_steps(
        payload,
        goal="звичка",
        days=7,
        tasks_per_day=2,
        preferred_hour="21:30",
        tz_name="Europe/Kyiv",
    )

    assert len(steps) == 14
    for day_index in range(7):
        day_steps = steps[day_index * 2: (day_index + 1) * 2]
        assert all(step["day"] == day_index + 1 for step in day_steps)
        assert all(step["message"] == "A" for step in day_steps)
        assert day_steps[0]["slot_index"] == 0
        assert day_steps[1]["slot_index"] == 1


def test_empty_steps_use_playbook():
    payload = {"steps": []}

    steps = normalize_plan_steps(
        payload,
        goal="сон",
        days=3,
        tasks_per_day=1,
        preferred_hour="20:00",
        tz_name="Europe/Kyiv",
    )

    expected_messages = PLAYBOOKS.get("сон") or _DEFAULT_PLAYBOOK
    assert len(steps) == 3
    messages = [step["message"] for step in steps]
    assert messages == expected_messages[:3]


def test_invalid_time_falls_back_to_default():
    steps = normalize_plan_steps(
        {"steps": [{"message": "A"}]},
        goal="звичка",
        days=1,
        tasks_per_day=1,
        preferred_hour="25:99",
        tz_name="Europe/Kyiv",
    )

    assert len(steps) == 1
    local_dt = steps[0]["proposed_for"].astimezone(pytz.timezone("Europe/Kyiv"))
    assert (local_dt.hour, local_dt.minute) == (21, 0)


def test_preferred_hours_list_applied():
    steps = normalize_plan_steps(
        {"steps": [{"message": "A"}, {"message": "B"}]},
        goal="звичка",
        days=1,
        tasks_per_day=1,
        preferred_hour="09:00",
        preferred_hours=["08:00", "14:30"],
        tz_name="Europe/Kyiv",
    )

    assert len(steps) == 2
    first_local = steps[0]["proposed_for"].astimezone(pytz.timezone("Europe/Kyiv"))
    second_local = steps[1]["proposed_for"].astimezone(pytz.timezone("Europe/Kyiv"))
    assert (first_local.hour, first_local.minute) == (8, 0)
    assert (second_local.hour, second_local.minute) == (14, 30)


def test_timezone_conversion_to_utc():
    steps = normalize_plan_steps(
        {"steps": [{"message": "A"}]},
        goal="звичка",
        days=1,
        tasks_per_day=1,
        preferred_hour="07:15",
        tz_name="Europe/Berlin",
    )

    assert len(steps) == 1
    proposed = steps[0]["proposed_for"]
    assert proposed.tzinfo == pytz.UTC
    local_dt = proposed.astimezone(pytz.timezone("Europe/Berlin"))
    assert local_dt.hour == 7
    assert local_dt.minute == 15

