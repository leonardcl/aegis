"""The Audit Council — a multi-agent deliberation inside Hermes.

AUDIT-FLOW.md describes a sequence of specialised checks. We run them as a
*council* of personas, each a Hermes call (or local fallback) armed with the
audit tools:

    ┌─ Reconciler        — RECONCILE (ledger vs Stripe)
    ├─ Compliance Officer — COMPLIANCE REPLAY (spend vs policy in force)
    ├─ Period Analyst     — PERIOD REVIEW + CATEGORIZE
    └─ Lead Auditor       — synthesises the three, runs full_audit, decides
                            REPORT + ESCALATE; LOOPS for another round if a
                            specialist asked for a deeper look.

Each persona runs its own tool-calling loop (see hermes_client.chat). The Lead
loops up to ``max_rounds`` times — re-tasking specialists while unresolved
escalations remain and progress is still being made — then finalises.

``run_council`` returns a structured result + a transcript the UI renders and
that gets persisted as an AuditReport (see audit_service.persist_council_result).
"""
import json
import re

from flask import current_app

from . import audit_engine, hermes_client, hermes_tools

MAX_ROUNDS = 2


def _cfg(key, default=None):
    try:
        return current_app.config.get(key, default)
    except RuntimeError:  # outside app context
        return default

_PERSONAS = {
    "reconciler": {
        "title": "Reconciler",
        "system": (
            "You are the Reconciler on an autonomous-finance audit council. Your "
            "job is RECONCILE: call gather_period then reconcile_ledger, then "
            "report matched counts and every exception (stripe_only rogue "
            "charges, ledger_only, amount_mismatch). Be specific with "
            "transaction ids and amounts. Recommend which items to escalate."
        ),
        "tools": ["gather_period", "reconcile_ledger"],
    },
    "compliance": {
        "title": "Compliance Officer",
        "system": (
            "You are the Compliance Officer. Your job is COMPLIANCE REPLAY: call "
            "compliance_replay and judge whether every posted spend stayed "
            "inside the policy in force. State the overall pass/fail, the policy "
            "version, and list each breach with its rule."
        ),
        "tools": ["compliance_replay"],
    },
    "period": {
        "title": "Period Analyst",
        "system": (
            "You are the Period Analyst. Your job is PERIOD REVIEW + CATEGORIZE: "
            "call period_review and categorize_spend. Surface spikes, repeat "
            "vendors, spend trends and the category/vendor breakdown. Flag waste."
        ),
        "tools": ["period_review", "categorize_spend"],
    },
    "advisor": {
        "title": "Cost Advisor",
        "system": (
            "You are the Cost Advisor. Your job is to ADVISE on every vendor, not "
            "only the flagged ones. Call recommend_actions and turn each entry "
            "into a clear, actionable suggestion: whether to keep, consolidate, "
            "negotiate, or investigate it, why, and the projected saving. Even "
            "for healthy spend, offer a small optimisation (e.g. annual billing). "
            "Call out which items are worth opening a negotiation on."
        ),
        "tools": ["recommend_actions"],
    },
    "lead": {
        "title": "Lead Auditor",
        "system": (
            "You are the Lead Auditor chairing the council. Read the specialists' "
            "findings, call full_audit to confirm the consolidated numbers, then "
            "write a tight executive summary: total spend, projected savings, "
            "reconciliation status, compliance result, and the exact items you "
            "are ESCALATING to the human queue with reasons. If a specialist "
            "left something unresolved, say what still needs a human."
        ),
        "tools": ["full_audit"],
    },
}


def _specs_for(persona_key):
    """Subset of TOOL_SPECS this persona is allowed to call."""
    allowed = set(_PERSONAS[persona_key]["tools"])
    return [t for t in hermes_tools.TOOL_SPECS
            if t["function"]["name"] in allowed]


def _run_persona(persona_key, period_days, extra_context=""):
    p = _PERSONAS[persona_key]
    user = (f"Audit the last {period_days} days. {extra_context}".strip())
    messages = [
        {"role": "system", "content": p["system"]},
        {"role": "user", "content": user},
    ]
    out = hermes_client.chat(messages, tools=_specs_for(persona_key),
                             label=persona_key)
    return {
        "persona": persona_key,
        "title": p["title"],
        "content": out.get("content", ""),
        "engine": out.get("engine"),
        "degraded_from": out.get("degraded_from"),
        "tool_calls": [{"name": c["name"], "arguments": c["arguments"]}
                       for c in out.get("tool_calls", [])],
    }


def _run_persona_ctx(app, persona_key, period_days, extra):
    """Run a persona inside its own app context (safe for worker threads)."""
    with app.app_context():
        return _run_persona(persona_key, period_days, extra)


def run_council(period_days=30, max_rounds=None, strategy=None):
    """Convene the audit council and return the deliberation + audit payload.

    Strategy (config ``HERMES_COUNCIL_STRATEGY``, default ``"auto"``):

      * ``"oneshot"``    — ONE Hermes call produces all five voices over a single
        shared ``full_audit`` payload. The Nemotron model server serialises
        requests (verified), so one call is ~5x faster than five and pays the
        ~14k-token gateway prompt overhead once instead of five times. Any
        section the model omits falls back to the deterministic narrator, so the
        transcript is always complete.
      * ``"hybrid"``     — the four reporting specialists are narrated
        deterministically (instant, and the numbers are deterministic anyway),
        and ONE Hermes call is spent on the Lead's executive synthesis — the part
        that actually benefits from model judgment. Fastest live path by far
        (~one call's latency), with real reasoning where it matters.
      * ``"sequential"`` — the classic one-call-per-persona loop (preserved for
        per-persona independence, A/B comparison, and as the offline path where
        the local reasoner is instant).
      * ``"auto"``       — oneshot when live, sequential when offline.

    Returns:
        dict: {"period_days", "rounds", "transcript", "audit", "engine"}
    """
    strategy = (strategy or _cfg("HERMES_COUNCIL_STRATEGY", "auto")
                or "auto").lower()
    if strategy == "auto":
        strategy = "oneshot" if hermes_client.is_live() else "sequential"
    if strategy == "hybrid":
        return _run_hybrid(period_days)
    if strategy == "oneshot":
        return _run_oneshot(period_days)
    return _run_sequential(period_days, max_rounds)


# --------------------------------------------------------------------------- #
# One-shot council — five voices from a single model call
# --------------------------------------------------------------------------- #
_ORDER = ["reconciler", "compliance", "period", "advisor", "lead"]

_SENTINEL = {"reconciler": "RECONCILER", "compliance": "COMPLIANCE",
             "period": "PERIOD", "advisor": "ADVISOR", "lead": "LEAD"}
_KEY_OF = {v: k for k, v in _SENTINEL.items()}

# Match a sentinel like @@RECONCILER@@. The capture is deliberately broad (any
# word) so a model wording variant — observed live: "RECONCILIATOR" for the
# reconciler, also "ADVISORY"/"ADVISER" — is still captured and resolved to a
# persona by prefix in _sentinel_key(), instead of silently dropping that voice
# and degrading it to the terse deterministic template.
_SECTION_RE = re.compile(r"@@\s*([A-Za-z]+)\s*@@")

# Prefix -> persona key, checked in order. Each is the unambiguous stem of the
# persona's name so near-misses (RECONCILIATOR, ADVISORY, COMPLIANT) still map.
_SENTINEL_PREFIXES = (
    ("reconcil", "reconciler"),
    ("complian", "compliance"),
    ("period", "period"),
    ("advis", "advisor"),
    ("lead", "lead"),
)


def _sentinel_key(token):
    """Resolve a captured sentinel word to a persona key by prefix, tolerating
    model variants. Returns None for an unrecognised sentinel."""
    t = (token or "").strip().lower()
    for prefix, key in _SENTINEL_PREFIXES:
        if t.startswith(prefix):
            return key
    return None


def _avail_results(audit):
    """Map tool name -> result, all extracted from ONE full_audit payload.

    ``full_audit`` already ran reconcile/compliance/review/categorize/recommend,
    so we reuse its sub-results instead of re-running each tool per persona
    (the sequential path runs the engine ~6x; the one-shot path runs it once).
    """
    return {
        "reconcile_ledger": audit["reconciliation"],
        "compliance_replay": audit["compliance"],
        "period_review": audit["period_review"],
        "categorize_spend": audit["categorization"],
        "recommend_actions": audit["advisory"],
        "full_audit": audit,
    }


def _persona_calls(persona_key, avail, period_days):
    """Tool-call records for a persona, sourced from the shared audit payload."""
    return [{"name": name, "arguments": {"period_days": period_days},
             "result": avail.get(name, {})}
            for name in _PERSONAS[persona_key]["tools"]]


def _council_context(audit):
    """Trim the full audit to the fields the council reasons over (token-lean)."""
    recon, comp = audit["reconciliation"], audit["compliance"]
    cats, adv = audit["categorization"], audit["advisory"]
    return {
        "reconciliation": {
            "status": recon["status"],
            "matched_count": recon["matched_count"],
            "exception_count": recon["exception_count"],
            "stripe_only": recon["stripe_only"],
            "ledger_only": recon["ledger_only"],
            "amount_mismatch": recon["amount_mismatch"],
        },
        "compliance": {
            "result": comp["result"], "policy_version": comp["policy_version"],
            "checked": comp["checked"], "passed": comp["passed"],
            "breaches": comp["breaches"],
        },
        "period_review": audit["period_review"],
        "categorization": {
            "by_category": cats["by_category"],
            "by_vendor": cats["by_vendor"][:8],
            "total_spend": cats["total_spend"],
        },
        "advisory": {
            "count": adv["count"], "negotiable_count": adv["negotiable_count"],
            "total_potential_savings": adv["total_potential_savings"],
            "recommendations": adv["recommendations"],
        },
        "headline": audit["headline"],
        "escalations": audit["escalations"],
    }


def _build_oneshot_messages(period_days, audit):
    system = (
        "You are the Aegis autonomous-finance AUDIT COUNCIL: five expert voices "
        "deliberating over one finance audit. You are given AUTHORITATIVE, "
        "pre-computed audit data. NEVER invent, drop, or alter a number — quote "
        "the figures exactly as given. Write tight, specific, decision-useful "
        "prose with transaction ids, payees and dollar amounts.\n\n"
        "Output EXACTLY five sections, in this order, each introduced by its "
        "sentinel ALONE on its own line, and NOTHING before, between (other than "
        "the section prose), or after them:\n"
        "@@RECONCILER@@ — ledger vs Stripe: matched count and EVERY exception "
        "(stripe_only rogue charges, ledger_only, amount_mismatch) with ids and "
        "amounts; say which to escalate.\n"
        "@@COMPLIANCE@@ — pass/fail vs the policy in force; state policy_version, "
        "passed/checked, and list each breach with its rule.\n"
        "@@PERIOD@@ — spikes, repeat vendors and trends, then the category / "
        "top-vendor breakdown; flag waste.\n"
        "@@ADVISOR@@ — for EVERY vendor give keep/consolidate/negotiate/"
        "investigate, the reason, and projected saving; call out which are worth "
        "negotiating.\n"
        "@@LEAD@@ — executive summary: total spend, projected savings, "
        "reconciliation status, compliance result, and the exact items ESCALATED "
        "to the human queue with reasons."
    )
    user = (f"Audit window: last {period_days} days.\n\n"
            f"AUTHORITATIVE DATA (JSON):\n```json\n"
            f"{json.dumps(_council_context(audit), default=str, indent=2)}\n```")
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def _parse_sections(text):
    """Split a one-shot reply into ``{persona_key: prose}``; tolerant of noise."""
    if not text:
        return {}
    parts = _SECTION_RE.split(text)
    out = {}
    # parts == [preamble, KEY, body, KEY, body, ...]
    for i in range(1, len(parts) - 1, 2):
        key = _sentinel_key(parts[i])
        body = (parts[i + 1] or "").strip()
        if key and body and key not in out:
            out[key] = body
    return out


def _run_oneshot(period_days):
    """All five voices from a single model call over one shared audit payload."""
    audit = audit_engine.full_audit(period_days)
    avail = _avail_results(audit)

    sections, degraded = {}, None
    if hermes_client.is_live():
        out = hermes_client.raw_complete(
            _build_oneshot_messages(period_days, audit),
            max_tokens=int(_cfg("HERMES_COUNCIL_MAX_TOKENS", 1600) or 1600),
            timeout=int(_cfg("HERMES_COUNCIL_TIMEOUT", 300) or 300),
            label="council")
        if out.get("content"):
            sections = _parse_sections(out["content"])
        degraded = out.get("degraded_from")

    live = hermes_client.is_live()
    transcript = []
    for key in _ORDER:
        calls = _persona_calls(key, avail, period_days)
        content = sections.get(key)
        if content:
            turn_engine, turn_degraded = "hermes", None
        else:
            content = hermes_client.narrate(key, calls)
            turn_engine = "local"
            turn_degraded = degraded or (
                "section missing from model reply" if live else None)
        transcript.append({
            "persona": key, "title": _PERSONAS[key]["title"],
            "content": content, "engine": turn_engine,
            "degraded_from": turn_degraded,
            "tool_calls": [{"name": c["name"], "arguments": c["arguments"]}
                           for c in calls],
            "round": 1,
        })

    engine = "hermes" if any(t["engine"] == "hermes" for t in transcript) \
        else "local"
    return {"period_days": period_days, "rounds": 1, "transcript": transcript,
            "audit": audit, "engine": engine}


# --------------------------------------------------------------------------- #
# Hybrid council — deterministic specialists + one Hermes lead synthesis
# --------------------------------------------------------------------------- #
def _build_lead_messages(period_days, audit, specialist_turns):
    """Prompt the Lead to synthesise the (deterministic) specialist findings."""
    findings = "\n\n".join(f"### {t['title']}\n{t['content']}"
                           for t in specialist_turns)
    ctx = {"headline": audit["headline"], "escalations": audit["escalations"]}
    system = (
        "You are the Lead Auditor chairing the council. The specialists have "
        "reported (below). Using their findings and the authoritative headline / "
        "escalation data, write a tight executive summary: total spend, projected "
        "savings, reconciliation status, compliance result, and the EXACT items "
        "you are escalating to the human queue with reasons. Quote numbers "
        "exactly; never invent figures. If something needs a human, say so."
    )
    user = (f"Audit window: last {period_days} days.\n\n"
            f"SPECIALIST FINDINGS:\n{findings}\n\n"
            f"AUTHORITATIVE HEADLINE / ESCALATIONS (JSON):\n```json\n"
            f"{json.dumps(ctx, default=str, indent=2)}\n```")
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def _run_hybrid(period_days):
    """Deterministic specialists + a single Hermes call for the Lead synthesis.

    Numbers are deterministic regardless of engine, so the four reporting voices
    are narrated locally (instant, exact). The one place model judgment adds value
    — weighing and summarising for the human — gets the single Hermes call.
    """
    audit = audit_engine.full_audit(period_days)
    avail = _avail_results(audit)

    transcript = []
    for key in _ORDER[:-1]:  # reconciler, compliance, period, advisor
        calls = _persona_calls(key, avail, period_days)
        transcript.append({
            "persona": key, "title": _PERSONAS[key]["title"],
            "content": hermes_client.narrate(key, calls), "engine": "local",
            "degraded_from": None,
            "tool_calls": [{"name": c["name"], "arguments": c["arguments"]}
                           for c in calls],
            "round": 1,
        })

    lead_calls = _persona_calls("lead", avail, period_days)
    lead_content, lead_engine, lead_degraded = None, "local", None
    if hermes_client.is_live():
        out = hermes_client.raw_complete(
            _build_lead_messages(period_days, audit, transcript),
            max_tokens=int(_cfg("HERMES_MAX_TOKENS", 400) or 400),
            timeout=int(_cfg("HERMES_TIMEOUT", 90) or 90), label="lead")
        if out.get("content"):
            lead_content, lead_engine = out["content"], "hermes"
        else:
            lead_degraded = out.get("degraded_from")
    if not lead_content:
        lead_content = hermes_client.narrate("lead", lead_calls)
    transcript.append({
        "persona": "lead", "title": _PERSONAS["lead"]["title"],
        "content": lead_content, "engine": lead_engine,
        "degraded_from": lead_degraded,
        "tool_calls": [{"name": c["name"], "arguments": c["arguments"]}
                       for c in lead_calls],
        "round": 1,
    })

    engine = "hermes" if any(t["engine"] == "hermes" for t in transcript) \
        else "local"
    return {"period_days": period_days, "rounds": 1, "transcript": transcript,
            "audit": audit, "engine": engine}


# --------------------------------------------------------------------------- #
# Sequential council — one model call per persona (classic path / offline)
# --------------------------------------------------------------------------- #
def _run_sequential(period_days=30, max_rounds=None):
    """Classic one-call-per-persona deliberation (see run_council docstring)."""
    live = hermes_client.is_live()
    if max_rounds is None:
        max_rounds = 1 if live else MAX_ROUNDS

    app = current_app._get_current_object()
    transcript = []
    specialists = ["reconciler", "compliance", "period", "advisor"]

    rounds_run = 0
    extra = ""
    for rnd in range(1, max_rounds + 1):
        rounds_run = rnd
        # Specialists run sequentially. The Nemotron server is single-threaded,
        # so concurrent calls only queue and risk per-call timeouts — sequential
        # gives each persona its full timeout window and is just as fast overall.
        for key in specialists:
            turn = _run_persona_ctx(app, key, period_days, extra)
            turn["round"] = rnd
            transcript.append(turn)

        # Lead synthesises this round.
        lead = _run_persona("lead", period_days,
                            "Synthesise the specialists' findings above.")
        lead["round"] = rnd
        transcript.append(lead)

        # Decide whether to loop: re-task only if exceptions remain AND we have
        # rounds left. (Deterministic stop condition — the council converges.)
        audit = audit_engine.full_audit(period_days)
        if audit["escalation_count"] == 0 or rnd >= max_rounds:
            break
        extra = (f"Round {rnd} left {audit['escalation_count']} unresolved "
                 f"exception(s); look closer at those transactions.")

    engine = "hermes" if any(t.get("engine") == "hermes" for t in transcript) \
        else "local"
    return {
        "period_days": period_days,
        "rounds": rounds_run,
        "transcript": transcript,
        "audit": audit,
        "engine": engine,
    }
