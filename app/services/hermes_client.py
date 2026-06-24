"""Client for the real Hermes Agent (Nous Research) OpenAI-compatible API.

Hermes exposes an OpenAI-compatible Chat Completions endpoint (default
``http://localhost:8642/v1``, bearer auth) backed by the NemoClaw gateway model
``nvidia/nemotron-3-super-120b-a12b``.

This module implements a **tool-calling loop**: send messages + tool specs, and
while the model returns ``tool_calls`` we execute them via ``hermes_tools`` and
feed the results back, until the model returns a normal assistant message.

If Hermes is not reachable (no URL configured, connection refused, timeout) we
fall back to a deterministic **local reasoner** that drives the same tools, so
the dashboard always produces a real, data-backed result for the demo.

Stdlib only (urllib) — no extra dependencies.
"""
import json
import urllib.error
import urllib.request

from flask import current_app

from . import hermes_tools

DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
MAX_TOOL_ROUNDS = 6


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #
def _cfg(key, default=""):
    try:
        return current_app.config.get(key, default)
    except RuntimeError:  # outside app context
        return default


def is_live():
    """True if a Hermes API URL is configured (we should attempt the real call)."""
    return bool(_cfg("HERMES_API_URL"))


# --------------------------------------------------------------------------- #
# Low-level HTTP call to the OpenAI-compatible endpoint
# --------------------------------------------------------------------------- #
def _post_chat(payload, timeout):
    base = _cfg("HERMES_API_URL").rstrip("/")
    url = f"{base}/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    api_key = _cfg("HERMES_API_KEY")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# Public: a single plain completion (no tools, no loop)
# --------------------------------------------------------------------------- #
def raw_complete(messages, max_tokens=None, timeout=None, label="oneshot"):
    """One model completion, no tool-calling loop.

    Used by the one-shot council, which has already computed every tool result
    deterministically and only needs the model to reason over them. Degrades
    gracefully: on any network/shape error (or when offline) it returns
    ``content=None`` plus ``degraded_from`` so the caller can fall back to the
    deterministic narrator per persona.

    Returns:
        dict: ``{"content": str|None, "engine": "hermes"|"local",
                 "degraded_from": str|None}``
    """
    if not is_live():
        return {"content": None, "engine": "local", "degraded_from": "offline"}
    model = _cfg("HERMES_MODEL") or DEFAULT_MODEL
    timeout = int(timeout or _cfg("HERMES_TIMEOUT", 90) or 90)
    max_tokens = int(max_tokens or _cfg("HERMES_MAX_TOKENS", 400) or 400)
    payload = {"model": model, "messages": list(messages),
               "temperature": 0.2, "max_tokens": max_tokens}
    try:
        resp = _post_chat(payload, timeout)
        msg = resp["choices"][0]["message"]
        return {"content": msg.get("content", "") or "", "engine": "hermes",
                "degraded_from": None, "label": label}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            KeyError, IndexError) as exc:
        return {"content": None, "engine": "local",
                "degraded_from": f"hermes unreachable: {exc}", "label": label}


def narrate(persona, calls):
    """Deterministic narration for one persona from pre-computed tool results.

    Public wrapper over the local narrator so the one-shot council can fall back
    per-persona when the live model omits or mangles a section. ``calls`` is a
    list of ``{"name", "result"}`` dicts (the same shape ``chat`` returns).
    """
    return _narrate(persona, calls)


# --------------------------------------------------------------------------- #
# Public: a single chat turn with automatic tool-calling loop
# --------------------------------------------------------------------------- #
def chat(messages, tools=None, use_tools=True, label="hermes"):
    """Run one conversation to completion, resolving tool calls along the way.

    Args:
        messages: OpenAI-style message list (system/user/assistant...).
        tools: tool specs to expose; defaults to the full audit tool set.
        use_tools: if False, no tools are offered (plain chat).
        label: persona label, recorded in the returned trace.

    Returns:
        dict: ``{"content": str, "trace": [...], "engine": "hermes"|"local",
                 "tool_calls": [{"name","arguments","result"}...]}``
    """
    tools = tools if tools is not None else (hermes_tools.TOOL_SPECS if use_tools else None)
    model = _cfg("HERMES_MODEL") or DEFAULT_MODEL
    timeout = int(_cfg("HERMES_TIMEOUT", 60) or 60)

    if is_live():
        try:
            return _chat_live(messages, tools, model, timeout, label)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                KeyError) as exc:  # network or shape error -> degrade gracefully
            fallback = _chat_local(messages, tools, label)
            fallback["degraded_from"] = f"hermes unreachable: {exc}"
            return fallback
    return _chat_local(messages, tools, label)


def _chat_live(messages, tools, model, timeout, label):
    """Route to native tool-calling or tool-augmented mode.

    This Hermes build's OpenAI adapter hangs when the ``tools`` parameter is
    sent, so by default (HERMES_NATIVE_TOOLS unset/false) we use tool-augmented
    mode: execute the tools deterministically, inject their JSON into the prompt,
    and let the real model reason over real data. Set HERMES_NATIVE_TOOLS=1 only
    if running against a build that supports OpenAI tool_calls.
    """
    native = str(_cfg("HERMES_NATIVE_TOOLS", "")).lower() in ("1", "true", "yes")
    if tools and not native:
        return _chat_augmented(messages, tools, model, timeout, label)
    return _chat_native(messages, tools, model, timeout, label)


def _chat_augmented(messages, tools, model, timeout, label):
    """Run the persona's tools on our side, inject results, get real reasoning."""
    max_tokens = int(_cfg("HERMES_MAX_TOKENS", 400) or 400)
    tool_names = [t["function"]["name"] for t in tools]

    tool_calls_made = []
    blocks = []
    for name in tool_names:
        result = hermes_tools.run_tool(name, {})
        tool_calls_made.append({"name": name, "arguments": {}, "result": result})
        blocks.append(f"### Tool `{name}` output\n```json\n"
                      f"{json.dumps(result, default=str, indent=2)}\n```")

    convo = list(messages)
    convo.append({
        "role": "user",
        "content": ("You have access to the following pre-computed tool results "
                    "(authoritative — do not invent numbers). Reason over them "
                    "and write your findings.\n\n" + "\n\n".join(blocks)),
    })
    payload = {"model": model, "messages": convo, "temperature": 0.2,
               "max_tokens": max_tokens}
    resp = _post_chat(payload, timeout)
    msg = resp["choices"][0]["message"]
    return {
        "content": msg.get("content", "") or "",
        "trace": convo, "engine": "hermes",
        "tool_calls": tool_calls_made, "label": label,
    }


def _chat_native(messages, tools, model, timeout, label):
    convo = list(messages)
    tool_calls_made = []
    for _ in range(MAX_TOOL_ROUNDS):
        payload = {"model": model, "messages": convo, "temperature": 0.2}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        resp = _post_chat(payload, timeout)
        msg = resp["choices"][0]["message"]
        convo.append(msg)

        calls = msg.get("tool_calls") or []
        if not calls:
            return {
                "content": msg.get("content", "") or "",
                "trace": convo, "engine": "hermes",
                "tool_calls": tool_calls_made, "label": label,
            }
        # Execute each requested tool and append results.
        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = hermes_tools.run_tool(name, args)
            tool_calls_made.append({"name": name, "arguments": args,
                                    "result": result})
            convo.append({
                "role": "tool", "tool_call_id": call.get("id", name),
                "name": name, "content": json.dumps(result, default=str),
            })
    # Ran out of rounds — return whatever the last assistant message held.
    last = next((m for m in reversed(convo) if m.get("role") == "assistant"), {})
    return {"content": last.get("content", "") or "", "trace": convo,
            "engine": "hermes", "tool_calls": tool_calls_made, "label": label}


# --------------------------------------------------------------------------- #
# Deterministic local fallback reasoner
# --------------------------------------------------------------------------- #
def _chat_local(messages, tools, label):
    """Stand-in for Hermes when the sandbox is unreachable.

    It inspects the system/user prompt for which tools to drive (by persona),
    executes them deterministically, and synthesises a narrative from the real
    engine output. The numbers are always real (engine-computed); only the prose
    is templated. Persona selection is keyword-based on the prompt text.
    """
    prompt = " ".join(m.get("content", "") or "" for m in messages
                      if isinstance(m.get("content"), str)).lower()

    # Decide which tools this persona should run.
    if any(k in prompt for k in ("advis", "recommend", "negotiate", "consolidate")):
        names = ["recommend_actions"]
        persona = "advisor"
    elif any(k in prompt for k in ("reconcil", "stripe", "rogue")):
        names = ["gather_period", "reconcile_ledger"]
        persona = "reconciler"
    elif any(k in prompt for k in ("complian", "policy", "mandate", "approval")):
        names = ["compliance_replay"]
        persona = "compliance"
    elif any(k in prompt for k in ("trend", "waste", "spike", "period", "categor")):
        names = ["period_review", "categorize_spend"]
        persona = "period"
    else:  # lead auditor / synthesis
        names = ["full_audit"]
        persona = "lead"

    tool_calls_made = []
    for name in names:
        result = hermes_tools.run_tool(name, {})
        tool_calls_made.append({"name": name, "arguments": {}, "result": result})

    content = _narrate(persona, tool_calls_made)
    return {"content": content, "trace": list(messages), "engine": "local",
            "tool_calls": tool_calls_made, "label": label}


def _narrate(persona, calls):
    """Build a human narrative from the tool results (local fallback only)."""
    results = {c["name"]: c["result"] for c in calls}
    if persona == "reconciler":
        r = results.get("reconcile_ledger", {})
        lines = [f"Reconciliation status: **{r.get('status', '?')}** — "
                 f"{r.get('matched_count', 0)} matched, "
                 f"{r.get('exception_count', 0)} exceptions."]
        for x in r.get("stripe_only", []):
            lines.append(f"⚠ Rogue charge {x['transaction_id']} to {x['payee']} "
                         f"(${x['amount']:,.0f}) has NO ledger entry — escalate.")
        for x in r.get("amount_mismatch", []):
            lines.append(f"⚠ Amount mismatch on {x['transaction_id']} "
                         f"({x['payee']}): ledger ${x['ledger_amount']:,.0f} vs "
                         f"Stripe ${x['stripe_amount']:,.0f}.")
        for x in r.get("ledger_only", []):
            lines.append(f"⚠ Ledger-only {x['transaction_id']} ({x['payee']}): "
                         f"recorded but unconfirmed by Stripe.")
        return "\n".join(lines)
    if persona == "compliance":
        c = results.get("compliance_replay", {})
        lines = [f"Compliance replay vs policy {c.get('policy_version')}: "
                 f"**{c.get('result', '?').upper()}** "
                 f"({c.get('passed', 0)}/{c.get('checked', 0)} passed)."]
        for b in c.get("breaches", []):
            lines.append(f"✗ {b['transaction_id']} ({b['payee']}, "
                         f"${b['amount']:,.0f}): {b['rule']}.")
        if not c.get("breaches"):
            lines.append("All posted spend stayed inside the mandate.")
        return "\n".join(lines)
    if persona == "period":
        pr = results.get("period_review", {})
        cat = results.get("categorize_spend", {})
        lines = [f"Period review: {pr.get('finding_count', 0)} findings across "
                 f"{pr.get('total_charges', 0)} charges "
                 f"(avg ${pr.get('average_charge', 0):,.0f})."]
        for f in pr.get("findings", []):
            lines.append(f"• {f['type']}: {f['note']}")
        top = cat.get("by_category", [])[:3]
        if top:
            lines.append("Top categories: " +
                         ", ".join(f"{t['category']} ${t['amount']:,.0f}"
                                   for t in top))
        return "\n".join(lines)
    if persona == "advisor":
        a = results.get("recommend_actions", {})
        lines = [f"Advisory: judged {a.get('count', 0)} vendors · "
                 f"{a.get('negotiable_count', 0)} worth negotiating · "
                 f"~${a.get('total_potential_savings', 0):,.0f} potential savings."]
        for r in a.get("recommendations", []):
            tag = {"keep": "✓", "consolidate": "↹", "negotiate": "⇄",
                   "investigate": "⚠"}.get(r["action"], "•")
            extra = (f" (save ~${r['projected_savings']:,.0f})"
                     if r["projected_savings"] else "")
            lines.append(f"{tag} {r['payee']} — {r['action']}{extra}: "
                         f"{r['rationale']}")
        return "\n".join(lines)
    # lead
    a = results.get("full_audit", {})
    h = a.get("headline", {})
    return (f"Audit complete. Total spend ${h.get('total_spend', 0):,.0f}, "
            f"projected savings ${h.get('projected_savings', 0):,.0f}. "
            f"Reconciliation: {h.get('reconciliation_status')}. "
            f"Compliance: {h.get('compliance_result')}. "
            f"{h.get('exceptions', 0)} item(s) escalated to the human queue.")
