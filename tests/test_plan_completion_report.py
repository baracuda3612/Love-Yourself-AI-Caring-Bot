from app.plan_completion.metrics import CompletionMetrics
from app.plan_completion.report import build_completion_report


def _metrics(**overrides) -> CompletionMetrics:
    data = {
        "plan_id": 1,
        "total_days": 21,
        "total_delivered": 42,
        "total_completed": 30,
        "total_skipped": 5,
        "total_ignored": 7,
        "completion_rate": 30 / 42,
        "best_streak": 3,
        "had_adaptations": False,
        "adaptation_count": 0,
        "dominant_time_slot": None,
        "focus": "mixed",
        "load": "MID",
        "duration": "STANDARD",
        "outcome_tier": "NEUTRAL",
    }
    data.update(overrides)
    return CompletionMetrics(**data)


def test_headers_all_outcomes_and_personas():
    cases = [
        ("STRONG", False, "motivator", "✅ 21 днів. 30/42 вправ виконано."),
        ("STRONG", False, "empath", "✅ План завершено."),
        ("STRONG", False, "rationalist", "✅ 21 днів / 30 з 42 вправ (71%)."),
        ("NEUTRAL", False, "motivator", "📋 21 днів пройдено."),
        ("NEUTRAL", False, "empath", "📋 21 днів. 30 вправ з 42 виконано."),
        ("NEUTRAL", False, "rationalist", "📋 21 днів. 71% виконання."),
        (
            "NEUTRAL",
            True,
            "motivator",
            "📋 21 днів. 30/42. Ти змінював план по дорозі — і дійшов.",
        ),
        (
            "NEUTRAL",
            True,
            "empath",
            "📋 21 днів. Ти адаптував план під себе 2 рази — і завершив.",
        ),
        (
            "NEUTRAL",
            True,
            "rationalist",
            "📋 21 днів / 71% / 2 адаптацій.",
        ),
        ("WEAK", False, "motivator", "📋 21 днів позаду."),
        (
            "WEAK",
            False,
            "empath",
            "📋 21 днів. 30 вправ з 42. Схоже, цей план і цей момент не збіглись.",
        ),
        ("WEAK", False, "rationalist", "📋 21 днів / 71%. Низький показник"),
    ]

    for outcome, had_adaptations, persona, expected_header_part in cases:
        report = build_completion_report(
            _metrics(
                outcome_tier=outcome,
                had_adaptations=had_adaptations,
                adaptation_count=2 if had_adaptations else 0,
            ),
            persona,
        )
        header_line = report.splitlines()[0]
        assert expected_header_part in header_line


def test_fallback_persona_defaults_to_empath():
    report = build_completion_report(_metrics(outcome_tier="STRONG"), "unknown")
    assert report.splitlines()[0].startswith("✅ План завершено.")


def test_observation_branch_rate_and_streak():
    report = build_completion_report(
        _metrics(completion_rate=0.9, best_streak=8, outcome_tier="STRONG"),
        "empath",
    )
    assert report.endswith("7+ днів поспіль — це вже не випадковість.")


def test_observation_branch_adapted_success():
    report = build_completion_report(
        _metrics(completion_rate=0.7, had_adaptations=True, adaptation_count=1),
        "empath",
    )
    assert report.endswith("Ти змінював план по дорозі — і це спрацювало.")


def test_observation_branch_morning_slot():
    report = build_completion_report(
        _metrics(completion_rate=0.61, dominant_time_slot="MORNING", best_streak=2),
        "empath",
    )
    assert report.endswith("Ранок виявився твоїм часом.")


def test_observation_branch_high_rate_only():
    report = build_completion_report(
        _metrics(completion_rate=0.87, best_streak=4, dominant_time_slot=None),
        "empath",
    )
    assert report.endswith("Ти тримав ритм навіть коли було складно.")


def test_observation_branch_streak_only():
    report = build_completion_report(
        _metrics(completion_rate=0.5, best_streak=7, dominant_time_slot=None),
        "empath",
    )
    assert report.endswith("Тиждень поспіль — перший реальний поріг пройдено.")


def test_observation_fallback_branch():
    report = build_completion_report(
        _metrics(total_days=13, completion_rate=0.4, best_streak=3, dominant_time_slot=None),
        "empath",
    )
    assert report.endswith("Ти пройшов 13 днів. Це більше ніж більшість.")


def test_stats_line_included_when_streak_or_adaptations_present():
    with_stats = build_completion_report(_metrics(best_streak=2, adaptation_count=1), "empath")
    assert "Streak: 2 дн. • адаптацій: 1" in with_stats

    without_stats = build_completion_report(
        _metrics(best_streak=0, adaptation_count=0),
        "empath",
    )
    assert "Streak:" not in without_stats
