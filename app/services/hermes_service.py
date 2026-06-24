"""Hermes Agent bridge.

The single seam between the Flask app and the Hermes Agent. Two entry points:

  * ``ask_hermes_agent(message, context)`` — the floating chatbot. Routes through
    the real Hermes client (with audit tools available) when HERMES_API_URL is
    configured, otherwise a deterministic keyword reply so the UI always works.

  * ``run_audit_council(period_days)`` — convene the audit council and persist a
    report. This is the AUDIT-FLOW capability the dashboard triggers.

Swapping to the real sandbox is config-only: set HERMES_API_URL / HERMES_API_KEY
(see docs/HERMES_AUDIT.md). No call sites change.
"""
import re

from . import agent_guardrail, audit_service, hermes_client, hermes_council


# --------------------------------------------------------------------------- #
# Chatbot
# --------------------------------------------------------------------------- #
def ask_hermes_agent(message, context=None):
    """Return a Hermes reply dict ``{"reply": str, "engine": str}``."""
    context = context or {}

    # If the user is asking to *run* an audit, convene the council in the
    # background (it makes several real Hermes calls and can take 1–2 min) and
    # point them at the Audit page, which polls for the result.
    if _is_audit_command(message):
        from flask import current_app
        from . import jobs
        period = _extract_period(message)
        job_id = jobs.start_audit_job(current_app._get_current_object(),
                                      period_days=period)
        return {"reply": (
            f"On it — I've convened the audit council for the last {period} days "
            f"(job `{job_id}`). The Reconciler, Compliance Officer and Period "
            f"Analyst are deliberating now, then the Lead Auditor synthesises. "
            f"Open the Audit page to watch it complete and see the full report."),
            "engine": "hermes" if hermes_client.is_live() else "local"}

    # Identity / capability questions get a deterministic, on-brand answer so the
    # opening demo beat is reliable and the agent never leaks its platform/model.
    identity = _identity_reply(message)
    if identity:
        return {"reply": identity,
                "engine": "hermes" if hermes_client.is_live() else "local"}

    if hermes_client.is_live():
        from flask import current_app
        page = context.get("page", "")
        messages = [
            {"role": "system", "content": agent_guardrail.SYSTEM_PROMPT
                + (f"\nThe user is on the '{page}' page." if page else "")},
            {"role": "user", "content": message or ""},
        ]
        # Plain conversation (no audit tools) — fast. Explicit audit commands are
        # handled above by the council. Reasoning/memory/etc. are unrestricted;
        # only *actions* are guardrailed (see agent_guardrail). Uses the shorter
        # chat timeout so the panel fails fast if the model is busy.
        chat_timeout = current_app.config.get("HERMES_CHAT_TIMEOUT", 30)
        out = hermes_client.chat(messages, use_tools=False, label="chat",
                                 timeout=chat_timeout)
        reply = out.get("content") or _keyword_reply(message)
        reply = agent_guardrail.screen_reply(message, reply)
        return {"reply": reply, "engine": out.get("engine", "hermes")}

    return {"reply": _keyword_reply(message), "engine": "local"}


_IDENTITY_Q = re.compile(
    r"\b(who\s+are\s+you|what\s+are\s+you|what\s+can\s+you\s+do|"
    r"what\s+do\s+you\s+do|introduce\s+yourself|your\s+name|are\s+you\s+(?:an?\s+)?"
    r"(?:ai|bot|model|llm)|what\s+model|which\s+model|how\s+were\s+you\s+(?:made|built|trained))\b",
    re.IGNORECASE)

_IDENTITY_REPLY = (
    "I'm Aegis — the autonomous CFO agent for this back office. I monitor spend "
    "against your budget, run procurement (discover vendors, score them on value, "
    "negotiate), keep an append-only ledger, and audit the books — reconciling "
    "against Stripe and replaying every spend against the policy in force. "
    "Crucially, I can't move money on my own: every spend passes a deterministic "
    "guardrail, and anything over the approval threshold goes to a human in the "
    "approval queue. Ask me about your budget, a procurement request, the pending "
    "approvals, or say \"run an audit\" and I'll convene the audit council."
)


def _identity_reply(message):
    """Return the canned identity/capability answer, or None if not such a question."""
    return _IDENTITY_REPLY if _IDENTITY_Q.search(message or "") else None


def _keyword_reply(message):
    """Deterministic offline reply (the original mock, kept as fallback)."""
    text = (message or "").lower()
    if any(k in text for k in ("approve", "guardrail", "approval")):
        return ("I reviewed the pending items. Two requests need human approval "
                "because they exceed the auto-approve ceiling. I recommend "
                "approving the highest-scorecard vendor and rejecting the "
                "duplicate request.")
    if any(k in text for k in ("vendor", "recommend", "scorecard")):
        return ("Based on your priority weights, the top vendor balances price "
                "and lead time best. I recommend sending it to the guardrail.")
    if any(k in text for k in ("audit", "ledger", "reconcil", "exception")):
        return ("Run the audit council from the Audit page and I'll reconcile "
                "the ledger against Stripe, replay compliance, and escalate any "
                "exceptions I find.")
    if any(k in text for k in ("budget", "spend", "savings")):
        return ("Monthly budget is healthy — tracking under ceiling with savings "
                "identified this month. Today's spend is within the daily limit.")
    return "I reviewed your request and recommend sending it to the guardrail."


# --------------------------------------------------------------------------- #
# Audit council
# --------------------------------------------------------------------------- #
def run_audit_council(period_days=30, persist=True):
    """Convene the audit council; optionally persist the resulting report.

    Returns:
        dict: {"result": <council dict>, "report_id": int|None}
    """
    result = hermes_council.run_council(period_days=period_days)
    report_id = None
    if persist:
        report = audit_service.persist_council_result(result)
        report_id = report.id
    return {"result": result, "report_id": report_id}


# --------------------------------------------------------------------------- #
# Audit-command detection for the chatbot
# --------------------------------------------------------------------------- #
_AUDIT_VERBS = ("run", "do", "start", "perform", "convene", "kick off", "execute")
_AUDIT_NOUNS = ("audit", "reconcile", "reconciliation", "compliance replay",
                "council")


def _is_audit_command(message):
    """True if the message asks to *run* an audit (vs. just chatting about one)."""
    text = (message or "").lower()
    if "audit council" in text or "run the audit" in text or "run an audit" in text:
        return True
    has_noun = any(n in text for n in _AUDIT_NOUNS)
    has_verb = any(v in text for v in _AUDIT_VERBS)
    # "audit the last 30 days", "reconcile now", etc.
    return has_noun and (has_verb or "last" in text or "now" in text)


def _extract_period(message):
    """Pull a day count out of the message; default 30."""
    m = re.search(r"(\d+)\s*(?:day|days|d)\b", (message or "").lower())
    if m:
        try:
            return max(1, min(int(m.group(1)), 365))
        except ValueError:
            pass
    return 30


def _format_audit_reply(outcome, period):
    """Concise chat summary of a council run, with a link to the full report."""
    a = outcome["result"]["audit"]
    h = a["headline"]
    rounds = outcome["result"]["rounds"]
    engine = outcome["result"]["engine"]
    lines = [
        f"Audit council complete ({engine}, {rounds} round(s)) for the last "
        f"{period} days:",
        f"• Total spend: ${h['total_spend']:,.0f} · projected savings "
        f"${h['projected_savings']:,.0f}",
        f"• Reconciliation: {h['reconciliation_status']} · Compliance: "
        f"{h['compliance_result']}",
        f"• {h['exceptions']} exception(s) escalated to the human queue.",
    ]
    for esc in a["escalations"][:4]:
        amt = esc.get("amount") or 0
        lines.append(f"   ⚠ {esc['exception_type']} {esc.get('transaction_id','')}"
                     f" (${amt:,.0f}) — {esc.get('note') or esc.get('rule','')}")
    lines.append("Open the Audit page for the full deliberation transcript.")
    return "\n".join(lines)
