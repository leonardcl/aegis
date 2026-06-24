"""Deterministic AUDIT-FLOW engine — the audit *substrate*.

Implements the seven steps from ``_TARGET_TODO/AUDIT-FLOW.md``:

    GATHER -> RECONCILE -> COMPLIANCE REPLAY -> PERIOD REVIEW
           -> CATEGORIZE -> REPORT -> ESCALATE

Design principle (mirrors HOW-IT-WORKS.md §2): the *numbers* are computed by
deterministic rules here, not guessed by the LLM. Hermes (and the council) call
these functions as tools and reason over their structured output. That keeps the
demo reproducible and the "viability" story honest — the agent narrates and
decides escalation, but it cannot fabricate a reconciliation result.

Every function takes plain arguments and returns plain JSON-serialisable dicts so
it can be exposed verbatim over the tool-calling interface (see hermes_tools.py).
"""
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func

from ..extensions import db
from ..models import LedgerEntry
from . import stripe_source

# --------------------------------------------------------------------------- #
# Policy snapshot (what was "in force"). Mirrors guardrail_service thresholds.
# For real point-in-time replay you would stamp each ledger row with the policy
# version in force; here we expose a single snapshot the replay reads against.
# --------------------------------------------------------------------------- #
POLICY = {
    "version": "2026.06-1",
    "auto_approve_limit": 5_000,
    "human_approval_limit": 50_000,
    "blocked_payees": {"unverified vendor", "sanctioned ltd"},
    # Outflow actions that actually move money (need a Stripe match + approval).
    "spend_actions": {"approve_spend", "pay_invoice", "create_subscription",
                      "topup_credits"},
}

# Lightweight category map by payee keyword (CATEGORIZE step).
_CATEGORY_RULES = [
    ("Cloud Infrastructure", ("aws", "cloud", "nimbus", "hyperscale", "datavault",
                               "warehouse", "vercel")),
    ("AI / APIs", ("openai", "anthropic", "nvidia", "gpu", "transcribe")),
    ("Developer Tools", ("github", "atlassian", "jira", "gitlab")),
    ("Productivity", ("slack", "notion", "zoom", "figma")),
    ("Security / Network", ("cloudflare", "okta", "snyk")),
    ("Compliance", ("audit", "meridian", "clearaudit", "soc")),
]


def _categorize_payee(payee):
    p = (payee or "").lower()
    for label, keys in _CATEGORY_RULES:
        if any(k in p for k in keys):
            return label
    return "Other"


# --------------------------------------------------------------------------- #
# 1. GATHER
# --------------------------------------------------------------------------- #
def gather(period_days=30):
    """Pull ledger entries, Stripe charges and the active policy for the period."""
    cutoff = datetime.utcnow() - timedelta(days=period_days)
    entries = (
        LedgerEntry.query.filter(LedgerEntry.timestamp >= cutoff)
        .order_by(LedgerEntry.timestamp.asc())
        .all()
    )
    ledger = [
        {
            "id": e.id,
            "transaction_id": e.transaction_id,
            "action": e.action,
            "payee": e.payee,
            "amount": float(e.amount or 0.0),
            "policy_decision": e.policy_decision,
            "outcome": e.outcome,
            "created_by": e.created_by,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
        }
        for e in entries
    ]
    charges = stripe_source.get_charges(period_days)
    charges_json = [
        {**c, "created_at": c["created_at"].isoformat()} for c in charges
    ]
    return {
        "period_days": period_days,
        "ledger_count": len(ledger),
        "stripe_count": len(charges_json),
        "ledger": ledger,
        "stripe_charges": charges_json,
        "policy": {k: (sorted(v) if isinstance(v, set) else v)
                   for k, v in POLICY.items()},
    }


# --------------------------------------------------------------------------- #
# 2. RECONCILE — ledger <-> Stripe
# --------------------------------------------------------------------------- #
def reconcile(period_days=30):
    """Match every ledger entry to a Stripe charge and vice-versa.

    Catches the three AUDIT-FLOW failure modes:
      * stripe_only    -> charge with no ledger entry (rogue / unauthorized)
      * ledger_only    -> recorded spend Stripe never confirmed
      * amount_mismatch -> ledger and Stripe disagree on the amount
    """
    cutoff = datetime.utcnow() - timedelta(days=period_days)
    entries = LedgerEntry.query.filter(LedgerEntry.timestamp >= cutoff).all()
    charges = stripe_source.get_charges(period_days)
    by_txn = stripe_source.index_by_txn(charges)

    matched, amount_mismatch, ledger_only = [], [], []
    seen_txns = set()

    for e in entries:
        # Only money-moving, successfully-posted rows should have a Stripe twin.
        is_spend = e.action in POLICY["spend_actions"] and e.outcome == "posted"
        if not is_spend:
            continue
        txn = e.transaction_id
        charge = by_txn.get(txn)
        if not charge:
            ledger_only.append({
                "transaction_id": txn, "payee": e.payee,
                "amount": float(e.amount or 0.0),
                "note": "ledger records a posted spend with no Stripe charge",
            })
            continue
        seen_txns.add(txn)
        if abs(float(charge["amount"]) - float(e.amount or 0.0)) > 0.01:
            amount_mismatch.append({
                "transaction_id": txn, "payee": e.payee,
                "ledger_amount": float(e.amount or 0.0),
                "stripe_amount": float(charge["amount"]),
                "delta": round(float(charge["amount"]) - float(e.amount or 0.0), 2),
                "note": "Stripe amount differs from ledger amount",
            })
        else:
            matched.append({"transaction_id": txn, "payee": e.payee,
                            "amount": float(e.amount or 0.0)})

    stripe_only = [
        {"transaction_id": c["transaction_id"], "payee": c["payee"],
         "amount": float(c["amount"]),
         "note": "Stripe charge with NO matching ledger entry — investigate"}
        for c in charges if c["transaction_id"] not in seen_txns
        and c["transaction_id"] not in {m["transaction_id"] for m in amount_mismatch}
    ]

    status = "balanced" if not (stripe_only or ledger_only or amount_mismatch) \
        else "discrepancy"
    return {
        "status": status,
        "matched": matched,
        "matched_count": len(matched),
        "stripe_only": stripe_only,
        "ledger_only": ledger_only,
        "amount_mismatch": amount_mismatch,
        "exception_count": len(stripe_only) + len(ledger_only) + len(amount_mismatch),
    }


# --------------------------------------------------------------------------- #
# 3. COMPLIANCE REPLAY — every spend vs the policy in force
# --------------------------------------------------------------------------- #
def compliance_replay(period_days=30):
    """Re-evaluate each posted spend against POLICY → pass/fail per transaction."""
    cutoff = datetime.utcnow() - timedelta(days=period_days)
    entries = (
        LedgerEntry.query.filter(LedgerEntry.timestamp >= cutoff)
        .order_by(LedgerEntry.timestamp.asc())
        .all()
    )
    results, breaches = [], []
    for e in entries:
        if e.action not in POLICY["spend_actions"]:
            continue
        amount = float(e.amount or 0.0)
        payee = (e.payee or "").strip().lower()
        verdict, rule = "pass", "within_policy"

        if payee in POLICY["blocked_payees"] and e.outcome == "posted":
            verdict, rule = "fail", "blocked_payee_was_paid"
        elif amount >= POLICY["human_approval_limit"] and e.outcome == "posted":
            verdict, rule = "fail", "exceeded_hard_cap"
        elif (amount >= POLICY["auto_approve_limit"] and e.outcome == "posted"
              and e.policy_decision == "ALLOW"):
            # Above approval threshold but recorded as auto-ALLOW = missing approval.
            verdict, rule = "fail", "missing_human_approval"

        row = {
            "transaction_id": e.transaction_id, "payee": e.payee,
            "amount": amount, "verdict": verdict, "rule": rule,
            "recorded_decision": e.policy_decision,
        }
        results.append(row)
        if verdict == "fail":
            breaches.append(row)

    overall = "pass" if not breaches else "fail"
    return {
        "result": overall,
        "checked": len(results),
        "passed": len(results) - len(breaches),
        "breaches": breaches,
        "rows": results,
        "policy_version": POLICY["version"],
    }


# --------------------------------------------------------------------------- #
# 4. PERIOD REVIEW — trends, spikes, duplicates over the whole window
# --------------------------------------------------------------------------- #
def period_review(period_days=30):
    """Run waste/anomaly rules across the period instead of one snapshot."""
    cutoff = datetime.utcnow() - timedelta(days=period_days)
    entries = LedgerEntry.query.filter(
        LedgerEntry.timestamp >= cutoff,
        LedgerEntry.outcome == "posted",
    ).all()

    by_payee = defaultdict(list)
    for e in entries:
        if e.action in POLICY["spend_actions"]:
            by_payee[e.payee].append(float(e.amount or 0.0))

    amounts = [a for v in by_payee.values() for a in v]
    avg = sum(amounts) / len(amounts) if amounts else 0.0
    findings = []

    # Spikes: any single charge > 3x the period average.
    for payee, vals in by_payee.items():
        for v in vals:
            if avg and v > 3 * avg:
                findings.append({
                    "type": "spike", "payee": payee, "amount": v,
                    "note": f"${v:,.0f} is >3x the period average (${avg:,.0f})",
                })

    # Duplicate vendors / repeated payees within the period.
    for payee, vals in by_payee.items():
        if len(vals) > 1:
            findings.append({
                "type": "repeat_vendor", "payee": payee, "count": len(vals),
                "amount": sum(vals),
                "note": f"{len(vals)} separate charges to {payee} this period",
            })

    return {
        "period_days": period_days,
        "total_charges": len(amounts),
        "average_charge": round(avg, 2),
        "findings": findings,
        "finding_count": len(findings),
    }


# --------------------------------------------------------------------------- #
# 5. CATEGORIZE — tag spend by category / vendor
# --------------------------------------------------------------------------- #
def categorize(period_days=30):
    """Tag each posted spend by category and roll up totals."""
    cutoff = datetime.utcnow() - timedelta(days=period_days)
    entries = LedgerEntry.query.filter(
        LedgerEntry.timestamp >= cutoff,
        LedgerEntry.outcome == "posted",
    ).all()

    by_category = defaultdict(float)
    by_vendor = defaultdict(float)
    for e in entries:
        if e.action not in POLICY["spend_actions"]:
            continue
        amt = float(e.amount or 0.0)
        by_category[_categorize_payee(e.payee)] += amt
        by_vendor[e.payee] += amt

    return {
        "by_category": [{"category": k, "amount": round(v, 2)}
                        for k, v in sorted(by_category.items(),
                                           key=lambda x: -x[1])],
        "by_vendor": [{"vendor": k, "amount": round(v, 2)}
                      for k, v in sorted(by_vendor.items(), key=lambda x: -x[1])],
        "total_spend": round(sum(by_category.values()), 2),
    }


# --------------------------------------------------------------------------- #
# Savings estimate (headline number) — projected, clearly labelled.
# --------------------------------------------------------------------------- #
def savings_estimate(period_days=30):
    """Annualised projected savings from blocked + cancellation actions."""
    cutoff = datetime.utcnow() - timedelta(days=period_days)
    blocked = (
        db.session.query(func.coalesce(func.sum(LedgerEntry.amount), 0.0))
        .filter(LedgerEntry.timestamp >= cutoff)
        .filter(LedgerEntry.outcome == "blocked")
        .scalar()
    )
    return {"projected_savings": round(float(blocked or 0.0), 2),
            "basis": "spend blocked by guardrail this period"}


# --------------------------------------------------------------------------- #
# 5b. RECOMMENDATIONS — judge every item, not just the exceptions
# --------------------------------------------------------------------------- #
# Vendors at/above this period total are candidates for negotiation.
NEGOTIATE_THRESHOLD = 5_000
# Vendors with repeat charges are candidates for consolidation.
CONSOLIDATE_MIN_CHARGES = 2


def recommendations(period_days=30):
    """Produce a judgment + suggested action for EVERY vendor in the period.

    Unlike escalations (which only cover exceptions), this judges normal spend
    too — so Hermes always has something to advise, e.g. "this looks fine, but
    you could negotiate an annual commit to save ~$X". Deterministic candidate
    logic; the Advisor persona narrates it.

    Verdicts: efficient | review | flagged
    Actions:  keep | consolidate | negotiate | investigate
    """
    cutoff = datetime.utcnow() - timedelta(days=period_days)
    entries = LedgerEntry.query.filter(
        LedgerEntry.timestamp >= cutoff,
        LedgerEntry.outcome == "posted",
    ).all()

    # Which payees are already flagged by reconciliation?
    recon = reconcile(period_days)
    flagged = {x["payee"] for x in recon["stripe_only"]} \
        | {x["payee"] for x in recon["amount_mismatch"]} \
        | {x["payee"] for x in recon["ledger_only"]}

    by_payee = defaultdict(lambda: {"total": 0.0, "count": 0})
    for e in entries:
        if e.action not in POLICY["spend_actions"]:
            continue
        by_payee[e.payee]["total"] += float(e.amount or 0.0)
        by_payee[e.payee]["count"] += 1

    recs = []
    for payee, agg in sorted(by_payee.items(), key=lambda x: -x[1]["total"]):
        total, count = agg["total"], agg["count"]
        category = _categorize_payee(payee)

        if payee in flagged:
            verdict, action = "flagged", "investigate"
            savings = 0.0
            rationale = ("Reconciliation flagged this payee — verify before any "
                         "further spend.")
        elif count >= CONSOLIDATE_MIN_CHARGES:
            verdict, action = "review", "consolidate"
            savings = round(total * 0.10, 2)
            rationale = (f"{count} separate charges this period (${total:,.0f}). "
                         f"Consolidate onto one plan / volume tier.")
        elif total >= NEGOTIATE_THRESHOLD:
            verdict, action = "review", "negotiate"
            savings = round(total * 0.12, 2)
            rationale = (f"${total:,.0f} this period — large enough to negotiate "
                         f"an annual commit or volume discount.")
        else:
            verdict, action = "efficient", "keep"
            savings = 0.0
            rationale = (f"${total:,.0f} this period — proportionate. Optionally "
                         f"switch to annual billing for a small saving.")

        # Value score: lower spend & not flagged → higher efficiency score.
        score = 9 if verdict == "efficient" else (6 if verdict == "review" else 3)

        recs.append({
            "payee": payee, "category": category,
            "total": round(total, 2), "charges": count,
            "verdict": verdict, "action": action,
            "value_score": score,
            "projected_savings": savings,
            "rationale": rationale,
            "negotiable": action == "negotiate",
        })

    total_potential = round(sum(r["projected_savings"] for r in recs), 2)
    return {
        "period_days": period_days,
        "recommendations": recs,
        "count": len(recs),
        "negotiable_count": sum(1 for r in recs if r["negotiable"]),
        "total_potential_savings": total_potential,
    }


# --------------------------------------------------------------------------- #
# 6 + 7. REPORT + ESCALATE — assemble the full audit payload
# --------------------------------------------------------------------------- #
def full_audit(period_days=30):
    """Run all steps and return one structured audit payload.

    This is what the council's Lead Auditor summarises and what gets persisted.
    Escalations = every reconciliation/compliance exception, with full context.
    """
    recon = reconcile(period_days)
    comp = compliance_replay(period_days)
    review = period_review(period_days)
    cats = categorize(period_days)
    savings = savings_estimate(period_days)
    recs = recommendations(period_days)

    escalations = []
    for x in recon["stripe_only"]:
        escalations.append({"exception_type": "stripe_only_charge", **x})
    for x in recon["ledger_only"]:
        escalations.append({"exception_type": "ledger_only_entry", **x})
    for x in recon["amount_mismatch"]:
        escalations.append({"exception_type": "amount_mismatch",
                            "amount": abs(x["delta"]), **x})
    for x in comp["breaches"]:
        etype = ("missing_approval" if x["rule"] == "missing_human_approval"
                 else "policy_violation")
        escalations.append({"exception_type": etype, **x})

    return {
        "period_days": period_days,
        "generated_at": datetime.utcnow().isoformat(),
        "reconciliation": recon,
        "compliance": comp,
        "period_review": review,
        "categorization": cats,
        "savings": savings,
        "advisory": recs,
        "total_spend": cats["total_spend"],
        "escalations": escalations,
        "escalation_count": len(escalations),
        "headline": {
            "total_spend": cats["total_spend"],
            "projected_savings": savings["projected_savings"],
            "reconciliation_status": recon["status"],
            "compliance_result": comp["result"],
            "exceptions": len(escalations),
        },
    }
