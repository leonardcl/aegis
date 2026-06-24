"""Mocked guardrail / policy decision logic.

Decides whether a procurement spend can proceed automatically, needs human
approval, or must be blocked outright. This is intentionally simple and
deterministic so the demo clearly shows the agent cannot spend freely.
"""
import os
from datetime import date, datetime

from flask import current_app

from ..extensions import db
from ..models import ApprovalRequest, LedgerEntry

# --------------------------------------------------------------------------- #
# Policy thresholds — the deterministic spend boundary. Tunable via env so a
# deployment can dial the mandate without code changes (see docs/GUARDRAILS.md).
# --------------------------------------------------------------------------- #
def _num_env(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return float(default)


AUTO_APPROVE_LIMIT = _num_env("AEGIS_AUTO_APPROVE_LIMIT", 5_000)      # <= -> ALLOW
PER_TRANSACTION_CAP = _num_env("AEGIS_PER_TRANSACTION_CAP", 50_000)   # >= single txn -> BLOCK
DAILY_BUDGET = _num_env("AEGIS_DAILY_BUDGET", 100_000)               # over -> NEEDS_APPROVAL
MONTHLY_BUDGET = _num_env("AEGIS_MONTHLY_BUDGET", 250_000)           # over -> BLOCK
# Back-compat alias (the per-transaction cap is the hard spend ceiling).
HUMAN_APPROVAL_LIMIT = PER_TRANSACTION_CAP

BLOCKED_PAYEES = {"unverified vendor", "sanctioned ltd"}

# Default-deny allowlist: the company's *vetted* payees, eligible for automatic
# payment (subject to cap + budget + approval threshold). A payee that is not on
# the allowlist and not blocked is treated as a NEW payee and routed to human
# approval for vetting — the agent never autonomously pays a stranger. Extend via
# AEGIS_ALLOWLIST="vendor a,vendor b"; disable enforcement with AEGIS_ALLOWLIST_ENABLED=0.
_DEFAULT_ALLOWLIST = {
    "aws", "nimbuscloud", "atlassian", "github", "slack", "cloudflare",
    "cloudflare inc", "datavault", "hyperscale", "openai", "anthropic", "nvidia",
    "vercel", "notion", "stripe", "figma", "zoom", "okta", "snyk", "gitlab",
    "meridian", "clearaudit", "google cloud", "datadog",
}
ALLOWLIST = _DEFAULT_ALLOWLIST | {
    p.strip().lower() for p in os.environ.get("AEGIS_ALLOWLIST", "").split(",")
    if p.strip()
}
ALLOWLIST_ENABLED = os.environ.get("AEGIS_ALLOWLIST_ENABLED", "1").lower() in (
    "1", "true", "yes", "on")


def _posted_spend(window):
    """Posted-outflow total for budget checks; 0.0 outside a db context."""
    from . import ledger_service
    try:
        # UTC: ledger timestamps are utcnow()-based, so "today"/"this month" must
        # be measured in UTC too (date.today() is local and drifts a day off
        # across the UTC boundary).
        utc_today = datetime.utcnow().date()
        if window == "day":
            return ledger_service.today_spend(utc_today)
        return ledger_service.month_spend(utc_today)
    except Exception:
        return 0.0


def _guardrails_disabled():
    """True only when GUARDRAILS_DISABLED is set (dev bypass). Safe outside an
    app context (returns False)."""
    try:
        return bool(current_app.config.get("GUARDRAILS_DISABLED"))
    except RuntimeError:
        return False


def evaluate_policy(amount, payee="", category=""):
    """Return a policy decision dict for a proposed spend.

    Returns:
        dict with keys: decision (ALLOW/NEEDS_APPROVAL/BLOCK), rule, reason.
    """
    # Development bypass — loud and reversible (see Config.GUARDRAILS_DISABLED).
    # This single early-return covers both send_to_guardrail and
    # agent_guardrail.check_action (which delegate here for money actions).
    if _guardrails_disabled():
        return {
            "decision": "ALLOW",
            "rule": "dev_bypass",
            "reason": "Guardrails disabled for development (GUARDRAILS_DISABLED).",
        }

    amount = float(amount or 0.0)
    payee_norm = (payee or "").strip().lower()

    # --- Hard BLOCK conditions (no human can override in-app) --------------- #
    if payee_norm in BLOCKED_PAYEES:
        return {
            "decision": "BLOCK", "rule": "payee_blocklist",
            "reason": f"Payee '{payee}' is on the blocklist and cannot be paid.",
        }

    if amount < 0:
        return {
            "decision": "BLOCK", "rule": "invalid_amount",
            "reason": "Amount cannot be negative.",
        }

    if amount >= PER_TRANSACTION_CAP:
        return {
            "decision": "BLOCK", "rule": "per_transaction_cap",
            "reason": (f"Amount ${amount:,.0f} meets/exceeds the per-transaction "
                       f"cap of ${PER_TRANSACTION_CAP:,.0f}. Escalation required."),
        }

    month_spent = _posted_spend("month")
    if month_spent + amount > MONTHLY_BUDGET:
        return {
            "decision": "BLOCK", "rule": "monthly_budget_exceeded",
            "reason": (f"${amount:,.0f} would push month-to-date spend "
                       f"(${month_spent:,.0f}) past the monthly budget of "
                       f"${MONTHLY_BUDGET:,.0f}."),
        }

    # --- NEEDS_APPROVAL conditions (a human may sign off) ------------------- #
    if ALLOWLIST_ENABLED and payee_norm and payee_norm not in ALLOWLIST:
        return {
            "decision": "NEEDS_APPROVAL", "rule": "payee_not_allowlisted",
            "reason": (f"'{payee}' is not on the vetted payee allowlist; a human "
                       f"must approve a first-time payee before any auto-payment."),
        }

    day_spent = _posted_spend("day")
    if day_spent + amount > DAILY_BUDGET:
        return {
            "decision": "NEEDS_APPROVAL", "rule": "daily_budget_exceeded",
            "reason": (f"${amount:,.0f} would push today's spend "
                       f"(${day_spent:,.0f}) past the daily budget of "
                       f"${DAILY_BUDGET:,.0f}; human sign-off required."),
        }

    if amount >= AUTO_APPROVE_LIMIT:
        return {
            "decision": "NEEDS_APPROVAL", "rule": "above_auto_approve_limit",
            "reason": (f"Amount ${amount:,.0f} is above the auto-approve limit of "
                       f"${AUTO_APPROVE_LIMIT:,.0f}; human sign-off required."),
        }

    # --- ALLOW: allowlisted payee, within cap, budget and approval threshold - #
    return {
        "decision": "ALLOW", "rule": "within_auto_approve_limit",
        "reason": (f"Amount ${amount:,.0f} is within the auto-approve limit for a "
                   f"vetted payee."),
    }


def send_to_guardrail(request):
    """Run a procurement request through the guardrail and create/update its
    ApprovalRequest. Returns the ApprovalRequest."""
    vendor = request.recommended_vendor or (request.vendors[0] if request.vendors else None)
    amount = vendor.price if vendor else (request.budget_ceiling or 0.0)
    payee = vendor.name if vendor else "TBD"

    # Use the negotiated price when a deal was agreed (W5).
    nego = vendor.negotiation if vendor else {}
    negotiated_note = ""
    if nego.get("agreed") and nego.get("agreed_amount"):
        sticker = amount
        amount = float(nego["agreed_amount"])
        if nego.get("savings"):
            negotiated_note = (
                f"Negotiated to ${amount:,.0f} from ${sticker:,.0f} "
                f"(saved ${nego['savings']:,.0f}). "
            )

    policy = evaluate_policy(amount, payee=payee, category=request.category)

    approval = request.approval or ApprovalRequest(request_id=request.id)
    approval.amount = amount
    approval.payee = payee
    approval.policy_decision = policy["decision"]
    approval.policy_rule = policy["rule"]
    approval.agent_reason = negotiated_note + policy["reason"]

    if policy["decision"] == "BLOCK":
        approval.status = "BLOCKED"
    else:
        approval.status = "NEEDS_APPROVAL"

    request.status = "sent_to_guardrail"

    if request.approval is None:
        db.session.add(approval)
    db.session.commit()
    return approval


def decide_approval(approval, decision, decided_by="management"):
    """Apply a human decision (approve/reject) to an ApprovalRequest and post a
    ledger entry recording the action.

    Status-guarded and idempotent: only a ``NEEDS_APPROVAL`` item can be decided.
    A double-submit (back button / double-click) or a hand-crafted POST trying to
    force-post a ``BLOCKED`` or already-decided item is a safe no-op — it returns
    the approval unchanged and posts nothing. This protects the exact guardrail
    invariant the product is built on: a blocked spend can never reach the ledger.
    """
    if approval.status != "NEEDS_APPROVAL" or approval.decided_at is not None:
        return approval

    decision = decision.lower()
    approval.decided_by = decided_by
    approval.decided_at = datetime.utcnow()

    request = approval.request

    if decision == "approve":
        approval.status = "APPROVED"
        outcome = "posted"
        action = "approve_spend"
        if request:
            request.status = "approved"
    else:
        approval.status = "REJECTED"
        outcome = "blocked"
        action = "reject_spend"
        if request:
            request.status = "rejected"

    entry = LedgerEntry(
        request_id=approval.request_id,
        action=action,
        payee=approval.payee,
        amount=approval.amount,
        reason=approval.agent_reason,
        policy_decision=approval.policy_decision,
        policy_rule=approval.policy_rule,
        outcome=outcome,
        # Stripe-style id with an "ch_aegis_" prefix so reconciliation can
        # recognise a spend Aegis authorised + posted through its own guardrail
        # and pair it with a confirmed Stripe twin (see stripe_source) instead of
        # flagging it as a false ledger-only discrepancy.
        transaction_id=f"ch_aegis_{approval.request_id}_{approval.id}",
        created_by=decided_by,
    )
    db.session.add(entry)
    db.session.commit()
    return approval
