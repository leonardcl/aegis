"""Phase 3 — the autonomous 'daily review' hero loop.

The agent reads the usage feed, flags waste + risk, and acts only within the
guardrail: cancel a zero-usage subscription (saving money) and top up a credit
about to run dry — all auto-executed because the guardrail ALLOWs them.
"""
from app.models import LedgerEntry
from app.services import autonomy, ledger_service


def test_analyze_flags_waste_and_risk():
    feed = {
        "subscriptions": [
            {"vendor": "Atlassian", "monthly_cost": 3400, "usage_30d": 1000},
            {"vendor": "DeadTool", "monthly_cost": 900, "usage_30d": 0},
        ],
        "credits": [
            {"service": "OpenAI", "balance_usd": 24, "burn_rate_day": 12, "topup_usd": 400},
            {"service": "AWS", "balance_usd": 9000, "burn_rate_day": 50, "topup_usd": 2000},
        ],
    }
    props = autonomy.analyze(feed)
    actions = {(p["action"], p["payee"]) for p in props}
    assert ("cancel_subscription", "DeadTool") in actions   # zero usage -> cancel
    assert ("topup_credits", "OpenAI") in actions           # ~2 days left -> topup
    assert ("topup_credits", "AWS") not in actions          # 180 days -> leave it
    assert ("cancel_subscription", "Atlassian") not in actions


def test_run_daily_review_executes_within_policy(app):
    with app.app_context():
        feed = {
            "subscriptions": [{"vendor": "DeadTool", "monthly_cost": 900, "usage_30d": 0}],
            "credits": [{"service": "OpenAI", "balance_usd": 24, "burn_rate_day": 12,
                         "topup_usd": 400}],
        }
        result = autonomy.run_daily_review(feed=feed)
        assert result["actions_taken"] == 2
        assert result["savings_month"] == 900
        # cancel recorded as a 'saved' row; topup as a posted spend.
        assert ledger_service.total_savings() == 900
        topups = LedgerEntry.query.filter_by(action="topup_credits").all()
        assert len(topups) == 1 and topups[0].outcome == "posted"
        assert topups[0].policy_decision == "ALLOW"


def test_zero_outflow_cancel_is_allowed_for_any_payee(app):
    """A cancellation moves no money, so it auto-approves even for an unlisted payee."""
    from app.services import guardrail_service
    with app.app_context():
        d = guardrail_service.evaluate_policy(0, payee="Some Unlisted Vendor")
        assert d["decision"] == "ALLOW" and d["rule"] == "no_outflow"
