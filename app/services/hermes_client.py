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
import logging
import socket
import threading
import time
import urllib.error
import urllib.request

from flask import current_app

from . import hermes_tools

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
MAX_TOOL_ROUNDS = 6

# --- Resilience knobs ------------------------------------------------------ #
# Retry only transient failures (timeouts / connection errors / 5xx), never
# 4xx. Backoff is exponential: 0.5s -> 1s -> 2s between the 3 attempts.
MAX_ATTEMPTS = 3
_BACKOFFS = (0.5, 1.0, 2.0)
# Circuit breaker: after this many *consecutive* failures we stop hammering the
# endpoint and short-circuit straight to the deterministic fallback for the
# cooldown window. A single success resets the counter.
CIRCUIT_THRESHOLD = 4
CIRCUIT_COOLDOWN = 30.0

_CB_LOCK = threading.Lock()
_CB = {"failures": 0, "open_until": 0.0}


class HermesUnavailable(urllib.error.URLError):
    """Raised when the circuit breaker is open (subclasses URLError so existing
    graceful-degradation ``except urllib.error.URLError`` handlers catch it)."""


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #
def _cfg(key, default=""):
    try:
        return current_app.config.get(key, default)
    except RuntimeError:  # outside app context
        return default


def _coerce_num(value, default, cast=int):
    """Coerce a (possibly non-numeric) config value, logging + defaulting on
    failure so a bad HERMES_TIMEOUT/HERMES_MAX_TOKENS never crashes a call."""
    try:
        return cast(value)
    except (TypeError, ValueError):
        logger.warning("Invalid numeric Hermes setting %r; using default %r",
                       value, default)
        return cast(default)


def is_live():
    """True if a Hermes API URL is configured (we should attempt the real call).

    Cheap config check only — it does NOT prove the model server is reachable.
    Use :func:`ping` for an actual reachability probe.
    """
    return bool(_cfg("HERMES_API_URL"))


def ping(timeout=2.0):
    """Actively probe whether the Hermes model server is reachable.

    Opens a TCP connection to the configured API host:port (no model call, so it
    never queues behind the single-threaded model and stays sub-second). Returns
    True only if the socket connects. Used by /healthz so "hermes_live" reflects
    real reachability, not just that a URL is set.
    """
    url = _cfg("HERMES_API_URL")
    if not url:
        return False
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Circuit breaker + retry helpers
# --------------------------------------------------------------------------- #
def _circuit_open():
    with _CB_LOCK:
        return bool(_CB["open_until"]) and time.monotonic() < _CB["open_until"]


def _record_success():
    with _CB_LOCK:
        _CB["failures"] = 0
        _CB["open_until"] = 0.0


def _record_failure():
    with _CB_LOCK:
        _CB["failures"] += 1
        if _CB["failures"] >= CIRCUIT_THRESHOLD:
            _CB["open_until"] = time.monotonic() + CIRCUIT_COOLDOWN
            logger.warning(
                "Hermes circuit breaker OPEN after %d consecutive failures; "
                "short-circuiting to fallback for %.0fs",
                _CB["failures"], CIRCUIT_COOLDOWN)


def _is_retryable(exc):
    """Retry transient transport failures and 5xx, but never 4xx client errors."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code is None or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError,
                            socket.timeout, OSError))


def _message_from_response(resp):
    """Validate the OpenAI-shaped response and return ``choices[0].message``.

    Raises ``ValueError`` on any malformed shape (not a dict, missing/empty
    ``choices``, missing ``message``) so callers degrade to the local narrator
    instead of throwing a raw KeyError/IndexError.
    """
    if not isinstance(resp, dict):
        raise ValueError(f"response is not a dict: {type(resp).__name__}")
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response missing a non-empty 'choices' list")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("choices[0] is not a dict")
    msg = first.get("message")
    if not isinstance(msg, dict):
        raise ValueError("choices[0].message missing or not a dict")
    return msg


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


def _post_chat_resilient(payload, timeout, label="hermes"):
    """``_post_chat`` with retry/backoff and a circuit breaker.

    Raises the last transport error (or ``HermesUnavailable`` when the breaker
    is open). The caller turns any raised error into graceful degradation.
    """
    if _circuit_open():
        raise HermesUnavailable(
            f"circuit open ({_CB['failures']} consecutive failures)")

    last_exc = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = _post_chat(payload, timeout)
            _record_success()
            return resp
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if not _is_retryable(exc):  # 4xx -> caller's bug, don't retry/trip
                logger.warning("Hermes %s HTTP %s (client error, no retry)",
                               label, exc.code)
                raise
            logger.warning("Hermes %s HTTP %s on attempt %d/%d",
                           label, exc.code, attempt, MAX_ATTEMPTS)
        except (urllib.error.URLError, TimeoutError, socket.timeout,
                OSError) as exc:
            last_exc = exc
            logger.warning("Hermes %s transport error on attempt %d/%d: %s",
                           label, attempt, MAX_ATTEMPTS, exc)
        if attempt < MAX_ATTEMPTS:
            time.sleep(_BACKOFFS[min(attempt - 1, len(_BACKOFFS) - 1)])

    _record_failure()
    raise last_exc


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
        logger.info("Hermes raw_complete[%s] path=offline", label)
        return {"content": None, "engine": "local", "degraded_from": "offline"}
    model = _cfg("HERMES_MODEL") or DEFAULT_MODEL
    timeout = _coerce_num(timeout or _cfg("HERMES_TIMEOUT", 90) or 90, 90)
    max_tokens = _coerce_num(max_tokens or _cfg("HERMES_MAX_TOKENS", 400) or 400, 400)
    payload = {"model": model, "messages": list(messages),
               "temperature": 0.2, "max_tokens": max_tokens}
    started = time.monotonic()
    try:
        resp = _post_chat_resilient(payload, timeout, label)
        msg = _message_from_response(resp)
        logger.info("Hermes raw_complete[%s] path=live engine=hermes %.2fs",
                    label, time.monotonic() - started)
        return {"content": msg.get("content", "") or "", "engine": "hermes",
                "degraded_from": None, "label": label}
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError,
            ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        logger.warning(
            "Hermes raw_complete[%s] path=fallback after %.2fs: %s",
            label, time.monotonic() - started, exc)
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
def chat(messages, tools=None, use_tools=True, label="hermes", timeout=None):
    """Run one conversation to completion, resolving tool calls along the way.

    Args:
        messages: OpenAI-style message list (system/user/assistant...).
        tools: tool specs to expose; defaults to the full audit tool set.
        use_tools: if False, no tools are offered (plain chat).
        label: persona label, recorded in the returned trace.
        timeout: per-call timeout (seconds); defaults to HERMES_TIMEOUT.

    Returns:
        dict: ``{"content": str, "trace": [...], "engine": "hermes"|"local",
                 "tool_calls": [{"name","arguments","result"}...]}``
    """
    tools = tools if tools is not None else (hermes_tools.TOOL_SPECS if use_tools else None)
    model = _cfg("HERMES_MODEL") or DEFAULT_MODEL
    timeout = _coerce_num(timeout or _cfg("HERMES_TIMEOUT", 60) or 60, 60)

    if is_live():
        started = time.monotonic()
        try:
            out = _chat_live(messages, tools, model, timeout, label)
            logger.info("Hermes chat[%s] path=live engine=hermes %.2fs",
                        label, time.monotonic() - started)
            return out
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError,
                ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
            # network or shape error -> degrade gracefully to the local narrator.
            logger.warning(
                "Hermes chat[%s] path=fallback after %.2fs: %s",
                label, time.monotonic() - started, exc)
            fallback = _chat_local(messages, tools, label)
            fallback["degraded_from"] = f"hermes unreachable: {exc}"
            return fallback
    logger.info("Hermes chat[%s] path=offline engine=local", label)
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
    max_tokens = _coerce_num(_cfg("HERMES_MAX_TOKENS", 400) or 400, 400)
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
    resp = _post_chat_resilient(payload, timeout, label)
    msg = _message_from_response(resp)
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
        resp = _post_chat_resilient(payload, timeout, label)
        msg = _message_from_response(resp)
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
