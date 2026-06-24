"""Hermes tool surface for the audit flow.

Exposes the deterministic ``audit_engine`` functions as OpenAI-compatible
function/tool specifications plus a dispatch table. Two consumers:

  1. hermes_client / hermes_council — pass TOOL_SPECS to the model so it can
     issue ``tool_calls``; dispatch them with ``run_tool``.
  2. The HTTP tool surface (routes/hermes_api.py) — lets the *real* Hermes
     sandbox call the very same tools over HTTP from inside its own runtime.

Keeping one definition for both means the offline council and the real sandbox
agent reason over identical tool contracts.
"""
from . import audit_engine

# --------------------------------------------------------------------------- #
# OpenAI-style tool specifications
# --------------------------------------------------------------------------- #
_PERIOD_PARAM = {
    "type": "object",
    "properties": {
        "period_days": {
            "type": "integer",
            "description": "Audit window in days (default 30).",
            "default": 30,
        }
    },
}


def _tool(name, description):
    return {
        "type": "function",
        "function": {"name": name, "description": description,
                     "parameters": _PERIOD_PARAM},
    }


TOOL_SPECS = [
    _tool("gather_period",
          "GATHER: pull ledger entries, Stripe charges and the active policy "
          "for the period. Always call this first."),
    _tool("reconcile_ledger",
          "RECONCILE: match ledger entries against Stripe charges. Returns "
          "matched, stripe_only (rogue charges), ledger_only, and "
          "amount_mismatch lists."),
    _tool("compliance_replay",
          "COMPLIANCE REPLAY: re-evaluate every posted spend against the policy "
          "in force. Returns pass/fail per transaction and a breaches list."),
    _tool("period_review",
          "PERIOD REVIEW: run waste/anomaly rules across the whole period — "
          "spikes, repeat vendors, trends."),
    _tool("categorize_spend",
          "CATEGORIZE: tag spend by category and vendor with rolled-up totals."),
    _tool("recommend_actions",
          "ADVISE: judge every vendor (keep/consolidate/negotiate/investigate) "
          "with a value score, rationale and projected savings — even items that "
          "are not exceptions. Marks which items are negotiable."),
    _tool("full_audit",
          "Run the entire flow (reconcile + compliance + review + categorize + "
          "savings + escalations) and return one consolidated payload."),
]

# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
_DISPATCH = {
    "gather_period": audit_engine.gather,
    "reconcile_ledger": audit_engine.reconcile,
    "compliance_replay": audit_engine.compliance_replay,
    "period_review": audit_engine.period_review,
    "categorize_spend": audit_engine.categorize,
    "recommend_actions": audit_engine.recommendations,
    "full_audit": audit_engine.full_audit,
}


def tool_names():
    return list(_DISPATCH.keys())


def run_tool(name, arguments=None):
    """Execute a tool by name. ``arguments`` is a dict (e.g. parsed tool_call).

    Returns the tool's JSON-serialisable result, or an ``{"error": ...}`` dict
    for an unknown tool so the model can recover instead of crashing.
    """
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}'",
                "available": list(_DISPATCH.keys())}
    args = arguments or {}
    period = int(args.get("period_days", 30) or 30)
    return fn(period_days=period)
