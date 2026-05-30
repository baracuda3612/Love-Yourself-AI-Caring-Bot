"""
FSM regression tests (T5.3 / T5.4).

Guards that must hold after adaptation flow and PLAN_FLOW tunnel removal:
- Adaptation states are not valid FSM states
- No transition into adaptation states is allowed
- Pause / resume / schedule-adjustment paths are unaffected
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test-user:test-pass@localhost:5432/test-db",
)
os.environ.setdefault("OPENAI_API_KEY", "test-key")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app.fsm.guards import can_transition
from app.fsm.states import is_valid_fsm_state


# ── Adaptation states are gone ────────────────────────────────────────────────

REMOVED_ADAPTATION_STATES = [
    "ADAPTATION_SELECTION",
    "ADAPTATION_PARAMS",
    "ADAPTATION_CONFIRMATION",
    "ADAPTATION_FLOW",
]

def test_adaptation_states_not_valid_fsm():
    for state in REMOVED_ADAPTATION_STATES:
        assert is_valid_fsm_state(state) is False, f"{state} should not be a valid FSM state"


def test_no_transition_into_adaptation_states():
    sources = ["ACTIVE", "ACTIVE_PAUSED", "IDLE_ONBOARDED", "IDLE_FINISHED"]
    for src in sources:
        for dst in REMOVED_ADAPTATION_STATES:
            assert can_transition(src, dst) is False, (
                f"can_transition({src!r}, {dst!r}) should be False"
            )


# ── PLAN_FLOW tunnel states are gone ──────────────────────────────────────────

REMOVED_PLAN_FLOW_STATES = [
    "PLAN_FLOW:DATA_COLLECTION",
    "PLAN_FLOW:CONFIRMATION_PENDING",
    "PLAN_FLOW:FINALIZATION",
]

def test_plan_flow_states_not_valid_fsm():
    for state in REMOVED_PLAN_FLOW_STATES:
        assert is_valid_fsm_state(state) is False, f"{state} should not be a valid FSM state"


# ── Live paths are unaffected ─────────────────────────────────────────────────

def test_pause_resume_transitions_still_work():
    assert can_transition("ACTIVE", "ACTIVE_PAUSED") is True
    assert can_transition("ACTIVE_PAUSED", "ACTIVE") is True


def test_plan_creation_transitions_still_work():
    for src in ("IDLE_ONBOARDED", "IDLE_FINISHED", "IDLE_DROPPED", "IDLE_PLAN_ABORTED"):
        assert can_transition(src, "ACTIVE") is True, f"can_transition({src!r}, 'ACTIVE') should be True"


def test_onboarding_completion_transition_still_works():
    assert can_transition("IDLE_NEW", "IDLE_ONBOARDED") is True
    assert can_transition("ONBOARDING:STEP_3", "IDLE_ONBOARDED") is True


def test_schedule_adjustment_transitions_still_work():
    assert can_transition("ACTIVE", "SCHEDULE_ADJUSTMENT") is True
    assert can_transition("ACTIVE_PAUSED", "SCHEDULE_ADJUSTMENT") is True
    assert can_transition("SCHEDULE_ADJUSTMENT", "ACTIVE") is True
    assert can_transition("SCHEDULE_ADJUSTMENT", "ACTIVE_PAUSED") is True


def test_onboarding_wildcard_is_valid_fsm_state():
    assert is_valid_fsm_state("ONBOARDING:START") is True
    assert is_valid_fsm_state("ONBOARDING:STEP_7") is True
