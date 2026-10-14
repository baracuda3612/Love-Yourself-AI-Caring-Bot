from dataclasses import replace
import pytest
from app.plan_completion.metrics import CompletionMetrics
from app.plan_completion.report import build_completion_report


def metrics(**changes):
    value=CompletionMetrics(1,14,28,14,7,7,.5,.75,.25,1,1,False,0,'DAY',None,None,'MEDIUM','NEUTRAL')
    return replace(value,**changes)


@pytest.mark.parametrize('days,exercises',[(7,7),(14,28)])
@pytest.mark.parametrize('completed',[0,1,7])
def test_summary_is_factual_for_both_formats_and_any_rate(days,exercises,completed):
    text=build_completion_report(metrics(total_days=days,total_delivered=exercises,total_completed=completed))
    assert f'{days} робочих днів' in text
    assert f'Виконано вправ: {completed} із {exercises}' in text
    for old in ['Streak','адаптац','більшість','Ранок','виявився','start_plan:']:
        assert old not in text
    assert 'Перша вправа прийде' not in text


def test_continuation_uses_prepared_real_datetime_only():
    expected='Перша вправа прийде 19.10.2026 о 14:00 (EEST).'
    assert expected in build_completion_report(metrics(),continuation=expected)


def test_persona_does_not_invent_observations():
    assert build_completion_report(metrics(),'rationalist')==build_completion_report(metrics(),'empath')
