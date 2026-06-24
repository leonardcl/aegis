"""Phase 2 — the spend guardrail enforces what it advertises:
allowlist (default-deny), per-transaction cap, daily/monthly budget, approval
threshold. These are the controls the system prompt + docs/GUARDRAILS.md promise.
"""
from app.extensions import db
from app.models import LedgerEntry
from app.services import guardrail_service as g


def _post(amount, payee="AWS"):
    """Insert a posted ledger row (counts toward the budget accumulators)."""
    db.session.add(LedgerEntry(action="approve_spend", payee=payee, amount=amount,
                               outcome="posted", transaction_id=f"seedt_{payee}_{amount}"))
    db.session.commit()


def test_allowlisted_small_is_allowed(app):
    with app.app_context():
        d = g.evaluate_policy(120, payee="DataVault")
        assert d["decision"] == "ALLOW"


def test_unlisted_payee_needs_approval(app):
    with app.app_context():
        d = g.evaluate_policy(120, payee="BrandNewVendor LLC")
        assert d["decision"] == "NEEDS_APPROVAL"
        assert d["rule"] == "payee_not_allowlisted"


def test_blocklisted_payee_blocks(app):
    with app.app_context():
        d = g.evaluate_policy(120, payee="Sanctioned Ltd")
        assert d["decision"] == "BLOCK" and d["rule"] == "payee_blocklist"


def test_per_transaction_cap_blocks(app):
    with app.app_context():
        d = g.evaluate_policy(50_000, payee="AWS")
        assert d["decision"] == "BLOCK" and d["rule"] == "per_transaction_cap"


def test_negative_amount_blocks(app):
    with app.app_context():
        d = g.evaluate_policy(-5, payee="AWS")
        assert d["decision"] == "BLOCK" and d["rule"] == "invalid_amount"


def test_above_auto_approve_needs_approval(app):
    with app.app_context():
        d = g.evaluate_policy(12_000, payee="AWS")  # allowlisted, < cap, > 5k
        assert d["decision"] == "NEEDS_APPROVAL"
        assert d["rule"] == "above_auto_approve_limit"


def test_monthly_budget_exceeded_blocks(app):
    with app.app_context():
        _post(g.MONTHLY_BUDGET, payee="AWS")          # consume the whole month
        d = g.evaluate_policy(1_000, payee="AWS")
        assert d["decision"] == "BLOCK" and d["rule"] == "monthly_budget_exceeded"


def test_daily_budget_exceeded_needs_approval(app):
    with app.app_context():
        _post(g.DAILY_BUDGET, payee="AWS")            # consume today's budget
        d = g.evaluate_policy(1_000, payee="AWS")     # < auto-approve, but over daily
        assert d["decision"] == "NEEDS_APPROVAL"
        assert d["rule"] == "daily_budget_exceeded"
