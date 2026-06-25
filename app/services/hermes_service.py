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
        import json
        from flask import current_app
        from . import hermes_tools
        page = context.get("page", "")

        # TOOL-AUGMENTED CHAT: pick the tools this question needs, run them over the
        # LIVE books, and let Hermes answer grounded in real data. (This build hangs
        # on native tool_calls, so we run tools server-side and inject the results —
        # same pattern as the council.)
        selected = _select_tools(message)
        used, blocks = [], []
        for name, args in selected:
            res = hermes_tools.run_tool(name, args)
            used.append(name)
            blocks.append(f"### `{name}`{(' ' + json.dumps(args)) if args else ''}\n"
                          f"```json\n{json.dumps(res, default=str)[:1600]}\n```")

        system = (agent_guardrail.SYSTEM_PROMPT
                  + (f"\nThe user is on the '{page}' page." if page else "")
                  + "\nYou have LIVE tools over the real books; their results are "
                    "below. Quote the real figures exactly; never invent numbers. "
                    "Be concise and specific.")
        user = (("LIVE TOOL RESULTS (authoritative):\n" + "\n\n".join(blocks) + "\n\n"
                 if blocks else "") + f"User: {message or ''}")

        chat_timeout = current_app.config.get("HERMES_CHAT_TIMEOUT", 30)
        out = hermes_client.raw_complete(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            timeout=chat_timeout, label="chat")
        reply = out.get("content") or _keyword_reply(message)
        reply = agent_guardrail.screen_reply(message, reply)
        return {"reply": reply, "engine": out.get("engine", "hermes"),
                "tools_used": used}

    return {"reply": _keyword_reply(message), "engine": "local"}


# --------------------------------------------------------------------------- #
# Tool routing for the chatbot — pick the tools a question needs (deterministic).
# --------------------------------------------------------------------------- #
def _money(num, suffix):
    try:
        v = float(str(num).replace(",", ""))
    except (ValueError, AttributeError, TypeError):
        return 0.0
    s = (suffix or "").lower()
    if s in ("k", "thousand"):
        v *= 1_000
    elif s in ("m", "million"):
        v *= 1_000_000
    return v


_PAYEE_RE = re.compile(
    r"(?:to|for|pay)\s+([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,2})")


def _guess_payee(message):
    m = _PAYEE_RE.search(message or "")
    return m.group(1).strip() if m else ""


def _select_tools(message):
    """Up to 3 tools relevant to the message (intent routing over the toolbox)."""
    t = (message or "").lower()
    tools = []

    def add(name, args=None):
        if name not in [x[0] for x in tools]:
            tools.append((name, args or {}))

    m = re.search(r"\$?\s*([0-9][0-9.,]*)\s*(k|m|thousand|million)?", t)
    if m and any(k in t for k in ("pay", "spend", "buy", "approve", "afford",
                                  "allowed", "can i", "charge", "purchase", "cost")):
        add("evaluate_spend", {"amount": _money(m.group(1), m.group(2)),
                               "payee": _guess_payee(message)})
    if any(k in t for k in ("budget", "remaining", "how much", "runway",
                            "over budget", "savings", "saved", "this month")):
        add("budget_status")
    if any(k in t for k in ("approval", "pending", "blocked", "queue", "waiting",
                            "sign off", "sign-off")):
        add("list_approvals")
    if any(k in t for k in ("policy", "limit", "cap", "allowlist", "blocklist",
                            "mandate", "rule", "threshold")):
        add("policy_summary")
    if any(k in t for k in ("ledger", "transaction", "recent", "history",
                            "charge", "paid")):
        add("ledger_recent", {"limit": 10})
    if any(k in t for k in ("category", "categories", "breakdown", "by vendor",
                            "biggest", "top vendor")):
        add("spend_breakdown")
    if any(k in t for k in ("find", "search", "cheaper", "alternative", "look up",
                            "research", "competitor", "vendor for")):
        add("web_search", {"query": message, "max_results": 5})
    if any(k in t for k in ("exception", "reconcil", "rogue", "mismatch",
                            "discrepan", "compliance")):
        add("full_audit")
    if not tools:
        add("financial_snapshot")     # default: situational awareness
    return tools[:3]


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
