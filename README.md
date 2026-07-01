# 🛡️ AEGIS — the Autonomous CFO Agent

> **What if your CFO never slept — and never went rogue?**

Built for the **Hermes Agent Accelerated Business Hackathon** (Stripe × NVIDIA ×
Nous Research), AEGIS is an agent that doesn't just *advise* on finance — it
**runs it, end to end**, with real spending power and hard safety rails.

Every dollar it moves passes a deterministic, default-deny guardrail it cannot
override. The thesis: *the agent isn't the hard part anymore — trusting it with a
credit card is.* **AEGIS solves trust.**

---

## 💼 What it does

**Procurement — it acts.**
Describe a need and AEGIS searches live vendors, scores every option, and
negotiates the price. Then, using **Stripe Skills**, it can actually buy what it
needs, provision its own SaaS, and pay for the services it uses — no human
copy-pasting card details. *Guardrail:* anything above your threshold stops for
human approval. Every time.

**Audit — it protects.**
A **5-persona audit council** reconciles your books against Stripe transaction
data in real time. In testing it caught a rogue charge and an overbill that a
human had missed — flagged automatically, with the receipts.

**Autonomy — it runs on its own.**
A one-click "daily review" ingests usage + credit data, flags zero-usage
subscriptions (cancel → savings) and credits about to run dry (top-up), runs each
through the **same** guardrail, and auto-executes only what the guardrail
`ALLOW`s — recording every action and the realized savings to an append-only
ledger.

---

## ⚙️ How it's built

- 🧠 **Cognition** — Hermes agent reasoning on **NVIDIA Nemotron** for fast,
  capable decision-making.
- 🛡️ **Safety** — **NVIDIA NeMo** guardrails keep the agent's actions inside the
  limits you define.
- 🚀 **Capabilities** — **NVIDIA Agent Skills / CUDA-X** for horsepower +
  **Stripe Skills** for real spending, SaaS provisioning, and payments.

Stack: **Flask · Jinja2 · Bootstrap 5 · SQLAlchemy · SQLite · Chart.js ·
Gunicorn** · Nous Hermes (Nemotron) via an OpenAI-compatible gateway.

---

## 🔑 The thesis

> **Constrain actions. Free cognition.**

Nemotron lets it *think* without limits. NeMo guardrails and human-approval gates
lock down what it's *allowed to do*. Stripe Skills give it a real wallet — on a
leash you control. A smarter agent inside a sound action-boundary is the goal.

---

## 🔗 Links

- 🎥 **Demo:** https://x.com/leonardchr88298/status/2072116761630212549
- 💻 **Code:** https://github.com/leonardcl/aegis
- 🚀 **Live app:** https://underground-cancellation-rabbit-adapted.trycloudflare.com/procurement/
  · user: `cfo` · pass: `1o-C1OJFVr1zY1vP`

*Feedback welcome 👇*

---

## What it actually does (not mocked)

- **Live multi-agent Audit Council.** Real **Nous Hermes / Nemotron** (via the
  Hermes gateway) convenes five expert voices — Reconciler, Compliance Officer,
  Period Analyst, Cost Advisor, Lead Auditor — over a deterministic 7-step audit
  engine. The *numbers* are computed by rules (the model can't fabricate them);
  the agents reason and decide escalation. Runs end-to-end live in well under two
  minutes, and degrades gracefully to a deterministic local reasoner if the model
  is unreachable, so the dashboard always produces a real result.
- **Autonomous "daily review" hero loop.** The agent ingests usage + credit data,
  flags zero-usage subscriptions and credits about to run dry, runs each through
  the **same** guardrail, and auto-executes only what the guardrail `ALLOW`s —
  recording every action and the realized savings to an append-only ledger.
- **The guardrail (the centerpiece).** A deterministic spend gate enforces a
  payee allowlist (default-deny → unlisted payees need human vetting), a
  per-transaction cap, daily/monthly budgets (read from the ledger), an
  auto-approve threshold, and a payee blocklist → `ALLOW` / `NEEDS_APPROVAL` /
  `BLOCK`. The mandate lives in `agent-cfo.policy.yaml` (out-of-process; the agent
  may *propose* edits, never apply them). Anything over the line is halted and
  escalated to a human approval queue.
- **On-demand procurement.** Intake → discover → enrich → evaluate (weighted
  scorecard) → agent-vs-agent negotiation → guardrail → ledger.
- **Audit & reconciliation.** Reconciles the ledger against Stripe (mock by
  default; flip `STRIPE_LIVE=1` for real test-mode), replays every spend against
  the policy in force, and catches rogue charges, amount mismatches, and missing
  approvals.
- **Floating Hermes chatbot** on every page, with identity hygiene (never leaks
  its model/platform) and a fast-failing timeout.

## Quickstart

```bash
python -m venv venv && source venv/bin/activate     # or use the conda env
pip install -r requirements.txt

cp .env.example .env                                 # configure (optional)
python seed.py                                       # create + populate the demo DB
python run.py                                        # http://localhost:5000
```

By default the app runs the **deterministic local reasoner** (no model needed).
To use the **live agent**, point it at a running Hermes gateway:

```bash
HERMES_API_URL=http://localhost:8642/v1 HERMES_API_KEY=... python run.py
```

## Production

```bash
gunicorn -w 1 -b 0.0.0.0:8000 run:app     # one worker: the model serializes calls
# or:  docker build -t aegis-cfo . && docker run -p 8000:8000 aegis-cfo
```

`FLASK_DEBUG` defaults **off** (the Werkzeug debugger is an RCE surface — never
enable it on a public host). For a publicly-exposed demo, set
`AEGIS_BASIC_AUTH=user:password` to gate the UI. Health probe at `/healthz`.

## Configuration

All knobs are environment variables (see `.env.example`). Highlights:

| Var | Purpose |
|-----|---------|
| `HERMES_API_URL` | Hermes gateway (`/v1`); empty → deterministic local reasoner |
| `agent-cfo.policy.yaml` | the spend mandate (limits + allowlist/blocklist) |
| `AEGIS_*_LIMIT` / `_BUDGET` | override individual policy limits |
| `GUARDRAILS_DISABLED=1` | **dev only** — bypass the spend gate (loud, default off) |
| `AEGIS_BASIC_AUTH` | shared-credential auth over the UI |
| `STRIPE_LIVE` / `STRIPE_SECRET_KEY` | reconcile against real Stripe test mode |

## Project layout

```
aegis-cfo/
├── agent-cfo.policy.yaml     # the spend mandate (out-of-process)
├── app/
│   ├── __init__.py           # app factory (auth, CSRF, warnings)
│   ├── config.py
│   ├── models.py
│   ├── routes/               # dashboard, procurement, approvals, audit, agent, hermes_api
│   ├── services/             # guardrail, autonomy (daily review), audit_engine,
│   │                         #   hermes_client/council/tools, procurement pipeline
│   ├── data/usage_feed.json  # mock usage feed for the daily review
│   ├── templates/  └─ static/
├── hermes/                   # SKILL.md + tool manifests for the sandboxed agent
├── tests/                    # offline / deterministic
├── seed.py · run.py · Dockerfile · ROADMAP.md
```

## Tests

```bash
HERMES_API_URL="" pytest -q        # fully offline / deterministic
```

## Docs

- `docs/GUARDRAILS.md` — the action-boundary design + tuning.
- `docs/HERMES_AUDIT.md` — the live council / tool integration.
- `ROADMAP.md` — current-state scorecard + the maturity plan.
- `DEMO_SCRIPT.md` — the 2-minute demo shot list.
