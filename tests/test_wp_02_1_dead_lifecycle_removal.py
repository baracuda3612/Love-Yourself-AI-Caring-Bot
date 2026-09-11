from __future__ import annotations

import ast
from pathlib import Path


APP_ROOT = Path("app")


def _runtime_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(APP_ROOT.rglob("*.py"))
    )


def test_retired_modules_and_dedicated_tests_are_deleted() -> None:
    for path in (
        APP_ROOT / "fsm" / "guards.py",
        APP_ROOT / "fsm" / "states.py",
        APP_ROOT / "plan_adaptations.py",
        Path("tests/test_fsm_states.py"),
        Path("tests/test_plan_flow_merge.py"),
        Path("tests/test_schedule_adjustment.py"),
        Path("tests/test_schedule_adjustment_timeout.py"),
    ):
        assert not path.exists(), f"retired compatibility surface still exists: {path}"


def test_runtime_has_no_retired_lifecycle_entrance_or_tunnel_symbol() -> None:
    source = _runtime_source()
    for marker in (
        "SCHEDULE_ADJUSTMENT",
        "schedule_adjustment_context",
        "sched_task:",
        "sched_time:",
        "sched_adj_timeout",
        "_resume_plan_if_paused",
        "generated_plan_object",
        "plan_updates",
        "transition_signal",
        "create_first_plan",
        "IDLE_ONBOARDED",
        "IDLE_DROPPED",
        "can_transition",
        "_guard_fsm_transition",
        "_commit_fsm_transition",
        "start_plan:",
    ):
        assert marker not in source, f"retired runtime marker remains: {marker}"


def test_workers_only_feed_supported_orchestrator_envelope_fields() -> None:
    allowed_fields = {
        "agent_name",
        "reply_type",
        "reply_text",
        "tool_call",
        "tool_calls",
        "usage",
        "debug",
        "error",
    }

    for path in sorted((APP_ROOT / "workers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            legacy = keys - allowed_fields
            assert not legacy.intersection(
                {"generated_plan_object", "plan_updates", "transition_signal"}
            ), f"legacy worker envelope in {path}: {legacy}"


def test_telegram_dispatcher_has_no_persistent_fsm_storage() -> None:
    source = (APP_ROOT / "telegram.py").read_text(encoding="utf-8")
    assert "Dispatcher(disable_fsm=True)" in source
    assert "create_fsm_storage" not in source


def test_scheduler_only_removes_the_retired_persisted_job() -> None:
    source = (APP_ROOT / "scheduler.py").read_text(encoding="utf-8")
    assert '"stuck_schedule_adj_check"' in source
    assert "check_stuck_schedule_adjustments" not in source
    assert "scheduler.add_job(\n        \"app.scheduler:check_stuck_schedule_adjustments\"" not in source
