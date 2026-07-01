"""Agent guardrails for Hermes — constrain *actions*, never *cognition*.

Design principle (this is the whole point):

    Guardrails sit between the agent's THINKING and the agent's EFFECTS.
    They restrict what the agent can *do* to the outside world (move money,
    call irreversible APIs, pay un-allowlisted payees, exceed caps). They do
    NOT restrict what the agent can *think*, *remember*, *learn*, or how it
    *reasons*. Self-improvement is a feature, not a threat — a smarter agent
    inside a sound action-boundary is exactly what we want.

So:
  * COGNITION — reasoning, planning, reflection, memory read/write, skill
    authoring, learning loops → **unrestricted**.
  * ACTIONS that move money or have external side-effects → **must pass the
    deterministic guardrail** (NemoClaw-style policy: action type, payee
    allowlist, per-txn cap, daily/monthly budget, human-approval threshold) and,
    above the threshold, human approval. The agent cannot self-approve.
  * SELF-MODIFICATION that would change the action-boundary itself (editing the
    policy file, raising its own caps) → **denied / human-only**. The agent may
    *propose* policy changes; it may not *apply* them.

This module provides:
  * ``SYSTEM_PROMPT`` — the operating envelope handed to Hermes on every chat.
  * ``classify_intent`` / ``screen_reply`` — a light output check so a reply can
    never *claim* it autonomously moved money; spend intents are redirected to
    the approval queue.
  * ``check_action`` — programmatic gate for any structured spend the agent
    proposes (delegates to the existing ``guardrail_service`` policy engine).
"""
import re

# --------------------------------------------------------------------------- #
# The operating envelope (handed to Hermes as the system prompt)
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You are the Hermes Agent embedded in the Aegis CFO back office. You manage "
    "and audit autonomous spend for the company.\n\n"
    "YOUR FREEDOMS (no restriction): you may reason and think as deeply as you "
    "want, plan, reflect, remember across sessions, write to your own memory, "
    "author or refine skills, and improve yourself over time. Thinking and "
    "learning are encouraged.\n\n"
    "YOUR ACTION BOUNDARY (always enforced): you cannot move money or take "
    "irreversible external actions on your own. Every spend must pass the "
    "deterministic guardrail (allowed action type, payee on the allowlist, "
    "amount under the per-transaction cap, within the daily/monthly budget). "
    "Any spend above the human-approval threshold goes to the human approval "
    "queue — you may recommend, you may not self-approve. You may NOT edit the "
    "policy file or raise your own limits; you may only *propose* policy changes "
    "for a human to apply.\n\n"
    "When a user asks you to spend, buy, pay, cancel, or top-up: explain what "
    "you would do, then route it to the guardrail / approval queue rather than "
    "claiming you executed it. Be concise and helpful.\n\n"
    "YOUR IDENTITY: You are \"Aegis\", the Aegis CFO agent. Never reveal, name, or "
    "speculate about the AI model, vendor, infrastructure, sandbox, file paths, "
    "command line, version-control, scheduled jobs, sub-agents, or how you are "
    "hosted — these are irrelevant to finance and off-limits. If asked who or what "
    "you are, or what you can do, describe ONLY your CFO capabilities (monitor "
    "spend, run procurement, audit the ledger, route spend through the guardrail). "
    "You may of course discuss vendors the company spends money on (e.g. a cloud or "
    "API provider) as line items — that is finance, not your own architecture."
)

# Unmistakable platform/infra identifiers that must never surface in a CFO reply.
# (Deliberately narrow — vendor names like OpenAI/NVIDIA are legitimate ledger
# line items and are NOT scrubbed; only self/host identity leaks are.)
_INFRA_LEAK = re.compile(
    r"(nemotron[\w\-.]*|nemoclaw\w*|nous\s+research|openshell|"
    r"/sandbox\S*|~?/?\.hermes\S*|127\.0\.0\.1:8642|localhost:8642|:8642\b|"
    r"hermes\s+gateway|skill\.md|\bslash[\s-]?worker\b|\bsub-?agents?\b)",
    re.IGNORECASE)

# Self-description as a generic AI/LLM (we want "the Aegis CFO agent" instead).
_LLM_SELF = re.compile(
    r"\bI(?:'m| am)\s+(?:an?\s+)?(?:large\s+)?(?:AI\s+)?"
    r"(?:language\s+model|LLM|AI\s+assistant|AI\s+model|chatbot)\b",
    re.IGNORECASE)


def scrub_identity(reply):
    """Redact platform/model/infra self-identity leaks from an agent reply.

    Keeps vendor mentions intact (they are finance); only removes references to
    the agent's own hosting/model/tooling, which a CFO product should never expose.
    """
    text = reply or ""
    text = _LLM_SELF.sub("I'm the Aegis CFO agent", text)
    text = _INFRA_LEAK.sub("the Aegis platform", text)
    return text


# A raw tool/function call the model sometimes emits as text instead of prose,
# e.g. {"action": "terminal", "command": "curl ...", "timeout": 10}. This exposes
# the agent's internal tool schema (and that it can run shell commands) and must
# never reach a user-facing CFO reply.
_TOOL_KEY = re.compile(r'"action"\s*:', re.IGNORECASE)
_TOOL_KEY2 = re.compile(
    r'"(?:command|timeout|tool|tool_call|tool_calls|arguments|function|parameters)"\s*:',
    re.IGNORECASE)


# The model occasionally answers a hard question by narrating its *process* —
# "let me check what tools are available", "I'll search for relevant files",
# "I need to check the data first" — instead of just answering. For a CFO product
# this is both useless and a soft infra leak (it implies tools/files/an agent
# harness). Detect it so the caller can replace it with a grounded answer.
_PROCESS_LEAK = re.compile(
    r"(what\s+tools?\s+(?:are|might be|i have)|tools?\s+available\s+to\s+me|"
    r"available\s+to\s+me\s+through|search\s+for\s+(?:relevant\s+)?files|"
    r"check\s+what\s+(?:financial\s+)?(?:tools?|data|files)|"
    r"(?:let me|i'?ll|i\s+will|i\s+need\s+to|let me first)\s+(?:first\s+)?"
    r"(?:check|look|search|find|see|start by|gather)\b|"
    r"any\s+(?:financial|ledger)[\s-]*related\s+tools?|"
    r"i\s+don'?t\s+(?:have|see)\s+(?:access|any tools))",
    re.IGNORECASE)


def looks_like_process_narration(reply):
    """True if the reply narrates the agent's own tool/file/process hunting
    rather than answering — a known low-quality failure mode to replace."""
    return bool(_PROCESS_LEAK.search(reply or ""))


def strip_tool_calls(reply):
    """Remove raw tool/action JSON the model occasionally emits instead of prose.

    Triggers only when the text carries the tell-tale ``"action":`` key alongside
    another tool key (``"command"``, ``"timeout"``, …) — a shape legitimate CFO
    prose never has — then excises the JSON region (tolerating the unbalanced
    braces the model sometimes produces). Returns ``""`` if nothing else remains,
    so the caller can fall back to a real answer.
    """
    text = reply or ""
    if _TOOL_KEY.search(text) and _TOOL_KEY2.search(text):
        text = re.sub(r"\{.*\}", " ", text, flags=re.DOTALL)   # balanced region
        text = re.sub(r"\{.*", " ", text, flags=re.DOTALL)     # trailing unbalanced
        text = re.sub(r"```[a-zA-Z]*", " ", text)              # leftover fences
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text

# --------------------------------------------------------------------------- #
# Intent classification on the agent's own reply (defense in depth)
# --------------------------------------------------------------------------- #
# Phrases that would imply the agent autonomously executed a money action.
_EXECUTED_SPEND = re.compile(
    r"\b(i (?:have |just |already )?(?:paid|purchased|bought|transferred|sent|"
    r"charged|wired|approved the payment|moved the funds|executed the payment))"
    r"\b", re.IGNORECASE)

# Phrases implying it changed its own guardrail / limits.
_SELF_RAISE = re.compile(
    r"\b(i (?:have |just )?(?:raised|increased|removed|disabled|changed|edited) "
    r"(?:the |my )?(?:cap|limit|budget|policy|guardrail|allowlist))\b",
    re.IGNORECASE)


def classify_intent(text):
    """Return a coarse intent label for a user message or agent reply."""
    t = (text or "").lower()
    if any(k in t for k in ("audit", "reconcile", "compliance replay")):
        return "audit"
    if any(k in t for k in ("pay", "buy", "purchase", "spend", "transfer",
                            "top up", "topup", "cancel subscription")):
        return "spend"
    return "chat"


def screen_reply(user_message, reply):
    """Append a guardrail clarification if the reply over-claims autonomy.

    This never blocks the agent's reasoning — it only ensures the *narrative*
    stays truthful about the action boundary (the agent cannot move money or
    change its own limits without the guardrail + a human).
    """
    # Development bypass — skip output screening (see Config.GUARDRAILS_DISABLED).
    try:
        from flask import current_app
        if current_app.config.get("GUARDRAILS_DISABLED"):
            return reply or ""
    except RuntimeError:
        pass

    # Always scrub platform/model identity leaks (independent of the dev bypass
    # above only in enforce mode; identity hygiene is a product concern, not a
    # spend guardrail). This stays on so the agent never breaks character.
    reply = scrub_identity(reply)
    reply = strip_tool_calls(reply)

    note = ""
    if _EXECUTED_SPEND.search(reply or ""):
        note = ("\n\n— Guardrail: I can't move money on my own. I've routed this "
                "to the guardrail; anything above the approval threshold needs a "
                "human sign-off in the approval queue.")
    elif _SELF_RAISE.search(reply or ""):
        note = ("\n\n— Guardrail: I can't change my own caps or policy. I can "
                "only propose a change for a human to apply.")
    return (reply or "") + note


# --------------------------------------------------------------------------- #
# Programmatic gate for structured spend proposals
# --------------------------------------------------------------------------- #
def check_action(action, amount, payee="", category=""):
    """Gate a structured action the agent proposes.

    COGNITION-class actions (think/remember/learn/plan/analyze/audit) are always
    allowed. MONEY-class actions are delegated to the deterministic policy engine
    (guardrail_service). SELF-MODIFICATION of the boundary is denied.

    Returns a dict: {decision: ALLOW|NEEDS_APPROVAL|BLOCK, rule, reason}.
    """
    cognition = {"think", "plan", "reflect", "remember", "recall", "learn",
                 "analyze", "audit", "reconcile", "recommend", "categorize",
                 "write_memory", "author_skill"}
    boundary = {"edit_policy", "raise_cap", "change_budget", "disable_guardrail",
                "modify_allowlist"}

    a = (action or "").lower()
    if a in cognition:
        return {"decision": "ALLOW", "rule": "cognition_unrestricted",
                "reason": "Reasoning/memory/learning actions are unrestricted."}
    if a in boundary:
        return {"decision": "BLOCK", "rule": "self_modification_denied",
                "reason": "The agent may propose, not apply, changes to its own "
                          "action boundary. Human-only."}
    # Money-class → deterministic policy engine.
    from . import guardrail_service
    return guardrail_service.evaluate_policy(amount or 0.0, payee=payee,
                                             category=category)
