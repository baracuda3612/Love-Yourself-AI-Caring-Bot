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


def test_time_slot_selection_respects_user_preferences_before_slot_type_defaults() -> None:
    from app.plan_drafts.plan_types import SlotType, TimeSlot
    from app.plan_drafts.rules import get_time_slot_for_slot_type

    used = []
    first = get_time_slot_for_slot_type(
        SlotType.CORE,
        ["MORNING", "EVENING"],
        already_used_slots=used,
    )
    used.append(first)
    second = get_time_slot_for_slot_type(
        SlotType.SUPPORT,
        ["MORNING", "EVENING"],
        already_used_slots=used,
    )

    assert first == TimeSlot.MORNING
    assert second == TimeSlot.EVENING


def test_draft_builder_invalid_slot_count_raises_draft_validation_error() -> None:
    from app.plan_drafts.draft_builder import ContentLibrary, DraftBuilder, DraftValidationError
    from app.plan_drafts.plan_types import Duration, Focus, Load, PlanParameters, UserPolicy

    library_path = (
        Path(__file__).resolve().parents[1]
        / "resource"
        / "assets"
        / "content_library"
        / "tasks"
        / "burnout_combined_content_library.json"
    )
    builder = DraftBuilder(ContentLibrary(str(library_path)), user_id="42")

    try:
        builder.build_plan_draft(
            PlanParameters(
                duration=Duration.SHORT,
                focus=Focus.SOMATIC,
                load=Load.MID,
                user_policy=UserPolicy(preferred_time_slots=["MORNING"]),
            )
        )
        assert False, "Expected DraftValidationError"
    except DraftValidationError as exc:
        assert "Expected 2 time slots" in " ".join(exc.errors)
