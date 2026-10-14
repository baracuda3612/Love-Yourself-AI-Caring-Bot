"""Legacy recommendation tests replaced by the automatic continuation contract.

The existing web recommendation engine is an explicit WP-07.4 deferral. It is
not used by Telegram completion, regardless of completion score or persona.
"""
from pathlib import Path
import pytest
from tests.test_plan_completion_report import metrics
from app.plan_completion.report import build_completion_report


@pytest.mark.parametrize('tier',['WEAK','NEUTRAL','STRONG'])
@pytest.mark.parametrize('duration,days',[('SHORT',7),('MEDIUM',14)])
def test_completion_never_recommends_a_different_format_or_a_start_button(tier,duration,days):
    text=build_completion_report(metrics(duration=duration,total_days=days,outcome_tier=tier))
    for deprecated in ['start_plan:', '21 д', '30 д', 'INTENSIVE', 'Обери', 'Спробувати легше']:
        assert deprecated not in text
    assert 'get_next_plan_recommendation' not in Path('app/plan_completion/workflow.py').read_text()
