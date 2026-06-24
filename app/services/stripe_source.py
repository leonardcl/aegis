"""Mock Stripe 'source of truth'.

In production this module would call the Stripe API (test mode). For the demo it
returns a deterministic list of charges representing *what actually happened* at
the payment processor — independent of what the agent recorded in the ledger.

The reconciliation step (audit_engine.reconcile) compares this against the
LedgerEntry table. The data below is deliberately seeded with three of the
failure modes AUDIT-FLOW.md calls out so the audit has something to catch:

  * stripe_only   -> a charge with NO matching ledger entry  (rogue / unauthorized)
  * amount_mismatch -> Stripe charged more than the ledger recorded
  * ledger_only   -> a cancellation the agent recorded that Stripe never confirmed
                     (handled on the ledger side; Stripe simply lacks the row)

Each charge is keyed by ``transaction_id`` so it can be joined to
``LedgerEntry.transaction_id``.
"""
from datetime import datetime, timedelta


def _now():
    return datetime.utcnow()


def get_charges(period_days=30):
    """Return the list of Stripe charges within the period.

    Returns:
        list[dict]: each ``{transaction_id, payee, amount, created_at, status}``.
    """
    now = _now()
    charges = [
        # --- Charges that DO match the seeded ledger (the happy path) ---------
        {"transaction_id": "txn_atl_014", "payee": "Atlassian", "amount": 3400,
         "created_at": now - timedelta(days=5), "status": "succeeded"},
        {"transaction_id": "txn_slk_009", "payee": "Slack", "amount": 2100,
         "created_at": now - timedelta(days=4), "status": "succeeded"},
        {"transaction_id": "txn_nim_201", "payee": "NimbusCloud", "amount": 12000,
         "created_at": now - timedelta(days=3), "status": "succeeded"},
        {"transaction_id": "txn_aws_330", "payee": "AWS", "amount": 8600,
         "created_at": now - timedelta(days=1), "status": "succeeded"},
        {"transaction_id": "txn_gh_120", "payee": "GitHub", "amount": 2400,
         "created_at": now - timedelta(hours=4), "status": "succeeded"},

        # --- AMOUNT MISMATCH: ledger recorded $1,200, Stripe charged $1,500 ---
        {"transaction_id": "txn_cf_001", "payee": "CloudFlare Inc", "amount": 1500,
         "created_at": now - timedelta(days=6), "status": "succeeded"},

        # --- STRIPE-ONLY: no ledger entry exists for this charge -------------
        # This is the "rogue charge" beat — spend the agent never recorded.
        {"transaction_id": "txn_str_555", "payee": "unknown-saas.io", "amount": 450,
         "created_at": now - timedelta(days=2), "status": "succeeded"},
    ]
    cutoff = now - timedelta(days=period_days)
    seeded = [c for c in charges if c["created_at"] >= cutoff]
    return seeded + _aegis_confirmed_charges(cutoff)


def _aegis_confirmed_charges(cutoff):
    """Confirmed Stripe twins for spends Aegis authorised through its guardrail.

    In production these are the matching objects in the real Stripe (test-mode)
    account. Here we derive them from the posted ledger so that an
    approve-then-audit flow reconciles cleanly instead of fabricating a
    ledger-only discrepancy. Only entries written by ``decide_approval`` (the
    ``ch_aegis_`` transaction-id prefix) are mirrored — the deliberately seeded
    rogue / mismatch / unconfirmed charges are untouched, so the demo exceptions
    still fire exactly as before. Restart-safe (no process state).
    """
    try:
        from ..models import LedgerEntry
        rows = (
            LedgerEntry.query.filter(
                LedgerEntry.outcome == "posted",
                LedgerEntry.transaction_id.like("ch_aegis_%"),
                LedgerEntry.timestamp >= cutoff,
            ).all()
        )
    except Exception:  # outside an app/db context (e.g. unit import) — no twins.
        return []
    return [
        {"transaction_id": r.transaction_id, "payee": r.payee,
         "amount": float(r.amount or 0.0),
         "created_at": r.timestamp or _now(), "status": "succeeded"}
        for r in rows
    ]


def index_by_txn(charges):
    """Index a charge list by transaction_id for O(1) reconciliation joins."""
    return {c["transaction_id"]: c for c in charges}
