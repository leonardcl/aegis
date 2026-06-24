"""Mocked guardrail / policy decision logic.

Decides whether a procurement spend can proceed automatically, needs human
approval, or must be blocked outright. This is intentionally simple and
deterministic so the demo clearly shows the agent cannot spend freely.
"""
from datetime import datetime

from ..extensions import db
from ..models import ApprovalRequest, LedgerEntry

# Policy thresholds
AUTO_APPROVE_LIMIT = 5_000      # below this, ALLOW
HUMAN_APPROVAL_LIMIT = 50_000   # below this, NEEDS_APPROVAL; at/above, BLOCK

BLOCKED_PAYEES = {"unverified vendor", "sanctioned ltd"}


def evaluate_policy(amount, payee="", category=""):
    """Return a policy decision dict for a proposed spend.

    Returns:
        dict with keys: decision (ALLOW/NEEDS_APPROVAL/BLOCK), rule, reason.
    """
    payee_norm = (payee or "").strip().lower()

    if payee_norm in BLOCKED_PAYEES:
        return {
            "decision": "BLOCK",
            "rule": "payee_blocklist",
            "reason": f"Payee '{payee}' is on the blocklist and cannot be paid.",
        }

    if amount >= HUMAN_APPROVAL_LIMIT:
        return {
            "decision": "BLOCK",
            "rule": "hard_spend_cap",
            "reason": (
                f"Amount ${amount:,.0f} exceeds the hard spend cap of "
                f"${HUMAN_APPROVAL_LIMIT:,.0f}. Escalation required."
            ),
        }

    if amount >= AUTO_APPROVE_LIMIT:
        return {
            "decision": "NEEDS_APPROVAL",
            "rule": "above_auto_approve_limit",
            "reason": (
                f"Amount ${amount:,.0f} is above the auto-approve limit of "
                f"${AUTO_APPROVE_LIMIT:,.0f}; human sign-off required."
            ),
        }

    return {
        "decision": "ALLOW",
        "rule": "within_auto_approve_limit",
        "reason": f"Amount ${amount:,.0f} is within the auto-approve limit.",
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
    ledger entry recording the action."""
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
        transaction_id=f"txn_{approval.request_id}_{approval.id}",
        created_by=decided_by,
    )
    db.session.add(entry)
    db.session.commit()
    return approval
