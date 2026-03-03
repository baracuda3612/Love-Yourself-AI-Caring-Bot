from app.plan_completion.cta import get_next_plan_recommendation
from app.plan_completion.metrics import CompletionMetrics


def _metrics(**overrides) -> CompletionMetrics:
    data = {
        "plan_id": 1,
        "total_days": 14,
        "total_delivered": 28,
        "total_completed": 20,
        "total_skipped": 4,
        "total_ignored": 4,
        "completion_rate": 20 / 28,
        "best_streak": 5,
        "had_adaptations": False,
        "adaptation_count": 0,
        "dominant_time_slot": "DAY",
        "focus": "mixed",
        "load": "MID",
        "duration": "MEDIUM",
        "outcome_tier": "NEUTRAL",
    }
    data.update(overrides)
    return CompletionMetrics(**data)


def test_strong_recommendation_load_up():
    rec = get_next_plan_recommendation(_metrics(outcome_tier="STRONG", load="MID"))
    assert rec.recommended_load == "INTENSIVE"
    assert rec.button1_text == "Піти далі →"
    assert rec.button1_params == {"duration": "MEDIUM", "load": "INTENSIVE", "focus": "mixed"}


def test_strong_intensive_recommends_duration_up():
    rec = get_next_plan_recommendation(
        _metrics(outcome_tier="STRONG", load="INTENSIVE", duration="STANDARD")
    )
    assert rec.recommended_duration == "LONG"
    assert rec.recommended_load == "INTENSIVE"
    assert rec.button1_text == "Поглибити →"
    assert rec.button1_params == {"duration": "LONG", "load": "INTENSIVE", "focus": "mixed"}


def test_neutral_without_adaptations_shorter_duration():
    rec = get_next_plan_recommendation(_metrics(outcome_tier="NEUTRAL", had_adaptations=False))
    assert rec.recommended_duration == "SHORT"
    assert rec.button1_text == "Почати коротший план"


def test_neutral_with_adaptations_load_down():
    rec = get_next_plan_recommendation(
        _metrics(outcome_tier="NEUTRAL", had_adaptations=True, load="INTENSIVE")
    )
    assert rec.recommended_load == "MID"
    assert rec.button1_text == "Спробувати легше"


def test_weak_with_load_above_lite():
    rec = get_next_plan_recommendation(
        _metrics(outcome_tier="WEAK", load="MID", focus="somatic")
    )
    assert rec.recommended_duration == "SHORT"
    assert rec.recommended_load == "LITE"
    assert rec.button2_text == "Змінити напрямок"
    assert rec.button2_params == {"duration": "SHORT", "load": "MID", "focus": "rest"}


def test_weak_with_lite_load_uses_short_lite_for_both_buttons():
    rec = get_next_plan_recommendation(
        _metrics(outcome_tier="WEAK", load="LITE", duration="LONG", focus="boundaries")
    )
    assert rec.recommended_duration == "SHORT"
    assert rec.recommended_load == "LITE"
    assert rec.recommended_focus == "rest"
    assert rec.button1_params == {"duration": "SHORT", "load": "LITE", "focus": "rest"}
    assert rec.button2_params == {
        "duration": "SHORT",
        "load": "LITE",
        "focus": "boundaries",
    }


def test_fallback_focus_and_load_defaults():
    rec = get_next_plan_recommendation(
        _metrics(outcome_tier="NEUTRAL", focus=None, load=None, duration=None)
    )
    assert rec.recommended_duration == "SHORT"
    assert rec.recommended_load == "LITE"
    assert rec.recommended_focus == "mixed"
    assert rec.button1_params == {"duration": "SHORT", "load": "LITE", "focus": "mixed"}


def test_non_canonical_load_and_duration_are_normalized():
    rec = get_next_plan_recommendation(
        _metrics(outcome_tier="STRONG", load="STANDARD", duration="2w", focus="cognitive")
    )
    assert rec.recommended_duration == "SHORT"
    assert rec.recommended_load == "MID"
    assert rec.button1_params == {"duration": "SHORT", "load": "MID", "focus": "cognitive"}
    assert rec.button2_params == {"duration": "SHORT", "load": "LITE", "focus": "cognitive"}
