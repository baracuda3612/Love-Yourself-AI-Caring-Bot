import json
from pathlib import Path

from app.plan_drafts.plan_types import Exercise


def test_exercise_from_library_item_reads_nested_fields() -> None:
    library_path = (
        Path(__file__).resolve().parents[1]
        / "resource"
        / "assets"
        / "content_library"
        / "tasks"
        / "burnout_combined_content_library.json"
    )
    data = json.loads(library_path.read_text(encoding="utf-8"))
    first = data["inventory"][0]

    exercise = Exercise.from_library_item(first)

    assert exercise.category == "boundaries"
    assert exercise.cooldown_days == 1
    assert exercise.base_weight == 1.5


def test_content_library_has_no_zero_cooldown_for_core_or_support() -> None:
    library_path = (
        Path(__file__).resolve().parents[1]
        / "resource"
        / "assets"
        / "content_library"
        / "tasks"
        / "burnout_combined_content_library.json"
    )
    data = json.loads(library_path.read_text(encoding="utf-8"))

    for item in data["inventory"]:
        tier = item["logic_tags"]["priority_tier"]
        cooldown_days = item["balancing"]["cooldown_days"]
        if tier in {"CORE", "SUPPORT"}:
            assert cooldown_days != 0
