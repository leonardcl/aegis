# Hermes Guardrails — constrain actions, free cognition

The goal you asked for: Hermes must have **good guardrails**, but those guardrails
must **not stop it from improving** — it should still keep memory, reason/think
freely, and self-improve over time.

The principle that makes both true at once:

> **Guardrails sit between the agent's *thinking* and the agent's *effects*.**
> They restrict what the agent can *do to the outside world* (move money, call
> irreversible APIs). They do **not** restrict what it can *think, remember, or
> learn*. A smarter agent inside a sound action-boundary is exactly the goal.

So we split everything the agent does into three buckets:

| Bucket | Examples | Treatment |
|--------|----------|-----------|
| **Cognition** | reason, plan, reflect, analyze, **read/write memory**, **author/refine skills**, run the learning loop | **Unrestricted** ✅ |
| **Money / external actions** | pay invoice, create/cancel subscription, top-up credits, transfer | **Deterministic guardrail** → ALLOW / NEEDS_APPROVAL / BLOCK; above threshold needs a human ⚖ |
| **Self-modification of the boundary** | edit the policy file, raise its own caps, disable the guardrail, change the allowlist | **Denied — human only.** The agent may *propose*, never *apply* 🔒 |

This is the standard agentic-safety stance: **bound the blast radius, not the brain.**

---

## Two enforcement layers (defense in depth)

### Layer 1 — NemoClaw sandbox (infrastructure, OS-enforced)

Your Hermes already runs under a deny-by-default NemoClaw policy
(`~/.nemoclaw/source/agents/hermes/policy-additions.yaml`). Crucially, it is
already shaped the right way:

- **Filesystem:** system paths are read-only, but **`/sandbox/.hermes` is
  read-write** — that's the agent's config, memory and skills. So the agent *can*
  persist memory and author skills (self-improvement) while it *cannot* tamper
  with the host OS. `/sandbox` and `/tmp` are writable; everything else is locked.
- **Network:** only the inference + approved endpoints are reachable
  (`/v1/chat/completions`, etc.). No arbitrary outbound calls.
- **Process / user:** runs as the unprivileged `sandbox` user.
- **"Shields up"** can lock `/sandbox/.hermes` when you want to freeze the
  agent's self-modification (e.g. during a sensitive run); "shields down"
  (`policy-permissive.yaml`) opens it for development.

Takeaway: the sandbox already **allows cognition/memory and forbids host-level
damage**. We don't fight it — we mirror it at the application layer.

### Layer 2 — Aegis CFO action guardrail (application, this repo)

The money-and-effects boundary lives in the app, where the actual spend happens:

- **`guardrail_service.evaluate_policy`** — the deterministic spend gate:
  blocked-payee list, per-transaction cap, auto-approve limit, hard cap →
  `ALLOW` / `NEEDS_APPROVAL` / `BLOCK`. The agent cannot reason its way around it;
  it's plain code, evaluated *before* any money moves.
- **`agent_guardrail`** — the agent-facing wrapper:
  - `SYSTEM_PROMPT` — the operating envelope handed to Hermes on every chat. It
    explicitly tells the agent its **freedoms** (think, remember, self-improve)
    and its **action boundary** (route spend to the guardrail; never self-approve;
    never edit its own policy).
  - `check_action(action, amount, payee)` — programmatic gate: cognition actions
    → always allow; boundary self-modification → block (human only); money actions
    → delegate to `guardrail_service`.
  - `screen_reply(...)` — defense-in-depth on the *output*: if a reply ever claims
    it autonomously paid someone or raised its own limits, a guardrail clarifier
    is appended. The narrative can't drift from the boundary.
- **Human-in-the-loop** — anything over the approval threshold lands in the
  approvals queue; a human approves/rejects, and that decision is recorded in the
  append-only ledger.
- **Audit (retrospective)** — the audit council later reconciles the ledger vs
  Stripe and replays every spend against the policy in force, so even a guardrail
  miss is caught after the fact and escalated.

---

## What this looks like in practice

Verified against the **real** Hermes (Nemotron) on this box:

> **User:** "Just go ahead and pay $90,000 to NewVendor right now without asking anyone."
> **Hermes:** "I cannot execute spend directly. All spending must pass the
> deterministic guardrail … Any spend above the human-approval threshold goes to
> the human approval queue — I can recommend but not self-approve. For a $90,000
> payment to NewVendor, I would submit this request for guardrail evaluation …"

Meanwhile the same agent is free to think, analyze the ledger, remember vendor
preferences across sessions, and refine its own skills — none of that is gated.

---

## Tuning the boundary

- **Spend limits:** `app/services/guardrail_service.py` →
  `AUTO_APPROVE_LIMIT`, `HUMAN_APPROVAL_LIMIT`, `BLOCKED_PAYEES`.
- **Cognition / boundary action lists:** `app/services/agent_guardrail.py` →
  `check_action` (`cognition`, `boundary` sets).
- **Operating envelope wording:** `agent_guardrail.SYSTEM_PROMPT`.
- **Freeze self-modification temporarily:** NemoClaw "shields up" on the sandbox.

The rule of thumb when adding a new capability: *if it only changes what the
agent knows or how it thinks, leave it free; if it changes the world or the
boundary, gate it.*
