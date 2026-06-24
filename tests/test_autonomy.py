"""Phase 3 — the autonomous 'daily review' hero loop.

The agent reads the usage feed, flags waste + risk, and acts only within the
guardrail: cancel a zero-usage subscription (saving money) and top up a credit
about to run dry — all auto-executed because the guardrail ALLOWs them.
"""
from app.models import LedgerEntry
from app.services import audit_engine, autonomy, ledger_service


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


def test_daily_review_is_idempotent(app):
    """Re-running the review must not duplicate actions or double-count savings."""
    with app.app_context():
        feed = {"subscriptions": [{"vendor": "DeadTool", "monthly_cost": 900, "usage_30d": 0}],
                "credits": []}
        first = autonomy.run_daily_review(feed=feed)
        second = autonomy.run_daily_review(feed=feed)
        assert first["actions_taken"] == 1
        assert second["actions_taken"] == 0 and second["savings_month"] == 0
        assert ledger_service.total_savings() == 900


def test_repeated_reviews_with_changed_feed_do_not_fabricate_discrepancy(app):
    """Two reviews with different feeds must not collide transaction ids and
    create a false reconciliation amount_mismatch (regression guard)."""
    with app.app_context():
        autonomy.run_daily_review(feed={
            "subscriptions": [], "credits": [
                {"service": "OpenAI", "balance_usd": 12, "burn_rate_day": 12, "topup_usd": 500}]})
        autonomy.run_daily_review(feed={
            "subscriptions": [], "credits": [
                {"service": "Anthropic", "balance_usd": 10, "burn_rate_day": 12, "topup_usd": 700}]})
        ids = [e.transaction_id for e in LedgerEntry.query.filter_by(created_by="agent").all()]
        assert len(ids) == len(set(ids)), f"duplicate agent txn ids: {ids}"
        recon = audit_engine.reconcile(30)
        aegis_mismatch = [x for x in recon["amount_mismatch"]
                          if x["transaction_id"].startswith("ch_aegis_")]
        assert not aegis_mismatch, f"fabricated mismatch: {aegis_mismatch}"


def test_zero_outflow_cancel_is_allowed_for_any_payee(app):
    """A cancellation moves no money, so it auto-approves even for an unlisted payee."""
    from app.services import guardrail_service
    with app.app_context():
        d = guardrail_service.evaluate_policy(0, payee="Some Unlisted Vendor")
        assert d["decision"] == "ALLOW" and d["rule"] == "no_outflow"
