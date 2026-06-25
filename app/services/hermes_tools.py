"""Hermes tool surface — the toolbox the agent can call.

Exposes deterministic functions as OpenAI-compatible tool specs + a dispatch
table, in three families:

  * AUDIT     — the 7-step audit engine (reconcile / compliance / review / ...).
  * WEB       — real web search, page crawl and URL liveness (services/websearch).
  * ANALYSIS  — situational awareness over the live books: budget status, ledger
                queries, spend breakdown, the approval queue, the policy in force,
                realized savings, and a guardrail "what-if" check.

Two consumers share one definition:
  1. hermes_client / hermes_council pass TOOL_SPECS to the model and dispatch
     ``tool_calls`` with ``run_tool``.
  2. routes/hermes_api.py lets the real sandboxed Hermes call the same tools over
     HTTP (POST /hermes/tools/<name> with a JSON arg body).

All handlers take a single ``args`` dict and never raise — a failure returns an
``{"error": ...}`` dict so the model can recover.
"""
from . import audit_engine, websearch


# --------------------------------------------------------------------------- #
# Arg coercion helpers (tolerant — the model's JSON args vary)
# --------------------------------------------------------------------------- #
def _int(a, key, default):
    try:
        return int(a.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _num(a, key, default=0.0):
    try:
        return float(str(a.get(key, default)).replace("$", "").replace(",", "") or default)
    except (TypeError, ValueError):
        return default


def _str(a, key, default=""):
    v = a.get(key, default)
    return str(v).strip() if v is not None else default


# --------------------------------------------------------------------------- #
# AUDIT handlers (period_days)
# --------------------------------------------------------------------------- #
def _period(fn):
    return lambda a: fn(period_days=_int(a, "period_days", 30))


# --------------------------------------------------------------------------- #
# WEB handlers
# --------------------------------------------------------------------------- #
def _h_web_search(a):
    q = _str(a, "query")
    res = websearch.web_search(q, max_results=_int(a, "max_results", 6))
    return {"query": q, "count": len(res), "results": res}


def _h_fetch_page(a):
    url = _str(a, "url")
    verdict, text = websearch.fetch_page_text(url, max_chars=_int(a, "max_chars", 4000))
    return {"url": url, "verdict": verdict, "chars": len(text), "text": text}


def _h_check_url(a):
    url = _str(a, "url")
    verdict, status = websearch.check_url(url)
    return {"url": url, "verdict": verdict, "status": status, "alive": verdict != "dead"}


# --------------------------------------------------------------------------- #
# ANALYSIS handlers (read the live books) — defensive about app context.
# --------------------------------------------------------------------------- #
def _h_budget_status(a):
    from datetime import datetime
    from . import guardrail_service as g, ledger_service as l
    today = datetime.utcnow().date()
    month, day = l.month_spend(today), l.today_spend(today)
    return {
        "monthly_budget": g.MONTHLY_BUDGET, "month_to_date": month,
        "monthly_remaining": round(g.MONTHLY_BUDGET - month, 2),
        "daily_budget": g.DAILY_BUDGET, "today_spent": day,
        "daily_remaining": round(g.DAILY_BUDGET - day, 2),
        "per_transaction_cap": g.PER_TRANSACTION_CAP,
        "auto_approve_limit": g.AUTO_APPROVE_LIMIT,
        "total_posted_spend": l.total_spend(), "realized_savings": l.total_savings(),
    }


def _h_ledger_recent(a):
    from .ledger_service import recent_entries
    limit, payee = _int(a, "limit", 15), _str(a, "payee").lower()
    rows, out = recent_entries(limit=200 if payee else limit), []
    for e in rows:
        if payee and payee not in (e.payee or "").lower():
            continue
        out.append({"timestamp": e.timestamp.isoformat() if e.timestamp else None,
                    "action": e.action, "payee": e.payee,
                    "amount": float(e.amount or 0.0), "outcome": e.outcome,
                    "policy_decision": e.policy_decision,
                    "transaction_id": e.transaction_id})
        if len(out) >= limit:
            break
    return {"count": len(out), "entries": out}


def _h_spend_breakdown(a):
    return audit_engine.categorize(period_days=_int(a, "period_days", 30))


def _h_list_approvals(a):
    from ..models import ApprovalRequest
    status = _str(a, "status").upper()
    q = ApprovalRequest.query
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(ApprovalRequest.created_at.desc()).limit(_int(a, "limit", 20)).all()
    return {"count": len(rows), "approvals": [
        {"id": r.id, "payee": r.payee, "amount": float(r.amount or 0.0),
         "status": r.status, "decision": r.policy_decision, "rule": r.policy_rule,
         "reason": r.agent_reason} for r in rows]}


def _h_policy_summary(a):
    from . import guardrail_service as g
    return {"auto_approve_limit": g.AUTO_APPROVE_LIMIT,
            "per_transaction_cap": g.PER_TRANSACTION_CAP,
            "daily_budget": g.DAILY_BUDGET, "monthly_budget": g.MONTHLY_BUDGET,
            "allowlist_enforced": g.ALLOWLIST_ENABLED,
            "allowlist": sorted(g.ALLOWLIST), "blocklist": sorted(g.BLOCKED_PAYEES)}


def _h_evaluate_spend(a):
    from . import guardrail_service as g
    return g.evaluate_policy(_num(a, "amount"), payee=_str(a, "payee"),
                             category=_str(a, "category"))


def _h_financial_snapshot(a):
    """SENSE: one combined situational-awareness payload for the agent."""
    p = _int(a, "period_days", 30)
    return {
        "budget": _h_budget_status(a),
        "headline": audit_engine.full_audit(p)["headline"],
        "pending_approvals": _h_list_approvals({"status": "NEEDS_APPROVAL", "limit": 10}),
        "blocked": _h_list_approvals({"status": "BLOCKED", "limit": 10}),
    }


# --------------------------------------------------------------------------- #
# Tool specs (OpenAI function schema) + dispatch
# --------------------------------------------------------------------------- #
def _spec(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       **({"required": required} if required else {})}}}


_PERIOD = {"period_days": {"type": "integer", "description": "Window in days (default 30)."}}

TOOL_SPECS = [
    # --- AUDIT ---
    _spec("gather_period", "GATHER: ledger entries, Stripe charges and the active "
          "policy for the period. Call first.", _PERIOD),
    _spec("reconcile_ledger", "RECONCILE: match ledger vs Stripe → matched, "
          "stripe_only (rogue), ledger_only, amount_mismatch.", _PERIOD),
    _spec("compliance_replay", "COMPLIANCE: re-check every posted spend against the "
          "policy in force → pass/fail + breaches.", _PERIOD),
    _spec("period_review", "PERIOD REVIEW: spikes, repeat vendors, trends across the "
          "window.", _PERIOD),
    _spec("categorize_spend", "CATEGORIZE: spend by category and vendor with totals.",
          _PERIOD),
    _spec("recommend_actions", "ADVISE: judge every vendor (keep/consolidate/"
          "negotiate/investigate) with savings.", _PERIOD),
    _spec("full_audit", "Run the entire audit flow and return one payload.", _PERIOD),
    # --- WEB / CRAWL ---
    _spec("web_search", "Search the live web (real results: title, url, snippet, "
          "domain). Use to research vendors, prices, alternatives.",
          {"query": {"type": "string", "description": "Search query."},
           "max_results": {"type": "integer", "description": "Default 6."}},
          ["query"]),
    _spec("fetch_page", "Crawl a URL and return its readable text (centred on price "
          "signals). Use to read a vendor's pricing page.",
          {"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"]),
    _spec("check_url", "Liveness of a URL: alive / blocked / dead (keeps bot-blocked "
          "real sites).", {"url": {"type": "string"}}, ["url"]),
    # --- ANALYSIS ---
    _spec("budget_status", "Current budget posture: monthly/daily budgets, spent, "
          "remaining, caps, realized savings."),
    _spec("ledger_recent", "Recent ledger entries; optional payee filter.",
          {"limit": {"type": "integer"}, "payee": {"type": "string"}}),
    _spec("spend_breakdown", "Spend grouped by category and vendor for the period.",
          _PERIOD),
    _spec("list_approvals", "The approval queue; optional status filter "
          "(NEEDS_APPROVAL / BLOCKED / APPROVED / REJECTED).",
          {"status": {"type": "string"}, "limit": {"type": "integer"}}),
    _spec("policy_summary", "The spend mandate in force: limits, allowlist, blocklist."),
    _spec("evaluate_spend", "What-if: run a proposed spend through the guardrail → "
          "ALLOW / NEEDS_APPROVAL / BLOCK with the rule.",
          {"amount": {"type": "number"}, "payee": {"type": "string"},
           "category": {"type": "string"}}, ["amount", "payee"]),
    _spec("financial_snapshot", "SENSE: one combined view — budget + audit headline "
          "+ pending and blocked approvals.", _PERIOD),
]

_DISPATCH = {
    # AUDIT
    "gather_period": _period(audit_engine.gather),
    "reconcile_ledger": _period(audit_engine.reconcile),
    "compliance_replay": _period(audit_engine.compliance_replay),
    "period_review": _period(audit_engine.period_review),
    "categorize_spend": _period(audit_engine.categorize),
    "recommend_actions": _period(audit_engine.recommendations),
    "full_audit": _period(audit_engine.full_audit),
    # WEB
    "web_search": _h_web_search,
    "fetch_page": _h_fetch_page,
    "check_url": _h_check_url,
    # ANALYSIS
    "budget_status": _h_budget_status,
    "ledger_recent": _h_ledger_recent,
    "spend_breakdown": _h_spend_breakdown,
    "list_approvals": _h_list_approvals,
    "policy_summary": _h_policy_summary,
    "evaluate_spend": _h_evaluate_spend,
    "financial_snapshot": _h_financial_snapshot,
}


def tool_names():
    return list(_DISPATCH.keys())


def run_tool(name, arguments=None):
    """Execute a tool by name with a dict of arguments. Never raises — returns an
    ``{"error": ...}`` dict for an unknown/failed tool so the model can recover."""
    handler = _DISPATCH.get(name)
    if handler is None:
        return {"error": f"unknown tool '{name}'", "available": list(_DISPATCH.keys())}
    try:
        return handler(arguments or {})
    except Exception as exc:  # noqa: BLE001 — surface to the model, don't crash
        return {"error": f"tool '{name}' failed: {exc}"}
