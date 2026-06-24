"""Autonomous 'daily review' loop — the P0 hero (HOW-IT-WORKS.md).

Runs SENSE -> ANALYZE -> PLAN -> GUARDRAIL -> EXECUTE -> RECORD over a usage feed:
the agent reads subscription usage + API-credit burn, flags waste (a zero-usage
subscription) and risk (a credit balance about to run dry), proposes actions, runs
each through the SAME deterministic guardrail every other spend uses, and
auto-executes only what the guardrail ALLOWs — recording every action (and the
savings) to the append-only ledger. Anything the guardrail escalates is left for a
human; nothing over the mandate is ever auto-executed.

This is the difference between "a dashboard that audits" and "an agent that finds
and fixes waste on its own." The detection is rule-assisted (deterministic), so the
demo is reproducible; the guardrail decision is the same plain code as everywhere.
"""
import json
import os
from datetime import datetime

from ..extensions import db
from ..models import LedgerEntry
from . import guardrail_service

_FEED_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "data", "usage_feed.json")

# Risk rule: top up an API credit projected to run dry within this many days.
CREDIT_RUNWAY_DAYS = 7


def load_usage_feed(path=None):
    """Load the mock usage feed (deterministic; swap for a real feed later)."""
    try:
        with open(path or _FEED_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"subscriptions": [], "credits": []}


def analyze(feed):
    """ANALYZE + PLAN: turn the feed into structured action proposals.

    Rule-assisted (not LLM-guessed) so it is reproducible:
      * subscription with 0 usage in 30 days  -> cancel (waste)
      * API credit projected to run dry < 7 days -> top up (risk)
    """
    proposals = []
    for s in feed.get("subscriptions", []):
        if int(s.get("usage_30d", 0)) == 0:
            cost = float(s.get("monthly_cost", 0))
            proposals.append({
                "action": "cancel_subscription", "payee": s["vendor"],
                "amount": 0.0, "savings_month": cost, "kind": "waste",
                "reason": (f"{s['vendor']} ({s.get('plan', '')}) — 0 usage in 30 "
                           f"days; cancelling saves ${cost:,.0f}/mo."),
            })
    for c in feed.get("credits", []):
        balance, burn = float(c.get("balance_usd", 0)), float(c.get("burn_rate_day", 0))
        days_left = (balance / burn) if burn else 999.0
        if days_left < CREDIT_RUNWAY_DAYS:
            topup = float(c.get("topup_usd", 500))
            proposals.append({
                "action": "topup_credits", "payee": c["service"],
                "amount": topup, "savings_month": 0.0, "kind": "risk",
                "reason": (f"{c['service']} credit ${balance:,.0f} burns in "
                           f"{days_left:.1f} days; top up ${topup:,.0f} to avoid a "
                           f"rate-limit outage."),
            })
    return proposals


def run_daily_review(feed=None, persist=True):
    """Run the full loop and return a structured summary.

    Returns dict with: proposals, executed/escalated/blocked lists (each carries
    the guardrail decision), savings_month, actions_taken.
    """
    feed = feed or load_usage_feed()
    proposals = analyze(feed)

    # Idempotency: skip a proposal the agent has already acted on in a prior
    # review, so re-running the loop doesn't duplicate ledger rows / double-count
    # savings.
    try:
        done = {(e.action, e.payee) for e in LedgerEntry.query
                .filter(LedgerEntry.created_by == "agent").all()}
    except Exception:
        done = set()
    proposals = [p for p in proposals if (p["action"], p["payee"]) not in done]

    executed, escalated, blocked = [], [], []
    savings_total = 0.0

    for i, p in enumerate(proposals):
        decision = guardrail_service.evaluate_policy(p["amount"], payee=p["payee"])
        rec = {**p, "decision": decision["decision"], "rule": decision["rule"],
               "decision_reason": decision["reason"]}

        if decision["decision"] == "ALLOW":
            # cancel = realized monthly saving (no money moves); topup = posted spend.
            is_cancel = p["action"] == "cancel_subscription"
            outcome = "saved" if is_cancel else "posted"
            ledger_amount = p["savings_month"] if is_cancel else p["amount"]
            if persist:
                db.session.add(LedgerEntry(
                    action=p["action"], payee=p["payee"], amount=ledger_amount,
                    reason=p["reason"], policy_decision="ALLOW",
                    policy_rule=decision["rule"], outcome=outcome,
                    transaction_id=f"ch_aegis_review_{i}", created_by="agent"))
            savings_total += p["savings_month"]
            executed.append(rec)
        elif decision["decision"] == "NEEDS_APPROVAL":
            escalated.append(rec)
        else:
            blocked.append(rec)

    if persist and executed:
        db.session.commit()

    return {
        "as_of": feed.get("as_of"),
        "ran_at": datetime.utcnow().isoformat(),
        "proposals": len(proposals),
        "executed": executed,
        "escalated": escalated,
        "blocked": blocked,
        "savings_month": savings_total,
        "actions_taken": len(executed),
    }
