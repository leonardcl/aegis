# Aegis CFO

**An autonomous AI back-office that monitors spend, runs procurement, audits its
own books — and physically cannot exceed the financial mandate you give it.**

Every dollar it moves passes a deterministic, default-deny guardrail it can't
override. The thesis: *the agent isn't the hard part anymore — trusting it with a
credit card is.* Aegis solves trust.

> **Principle: constrain actions, free cognition.** The agent reasons, remembers
> and self-improves without limit; only its *effects on the world* (moving money)
> are gated. A smarter agent inside a sound action-boundary is the goal.

Built for the Hermes Agent Accelerated Business Hackathon (NVIDIA × Stripe × Nous
Research) with Flask · SQLAlchemy · Bootstrap 5 · Chart.js.

---

## What it actually does (not mocked)

- **Live multi-agent Audit Council.** Real **Nous Hermes / Nemotron-3** (via the
  Hermes gateway) convenes five expert voices — Reconciler, Compliance Officer,
  Period Analyst, Cost Advisor, Lead Auditor — over a deterministic 7-step audit
  engine. The *numbers* are computed by rules (the model can't fabricate them); the
  agents reason and decide escalation. Runs end-to-end live in well under two
  minutes, and degrades gracefully to a deterministic local reasoner if the model
  is unreachable, so the dashboard always produces a real result.
- **Autonomous "daily review" hero loop.** The agent ingests usage + credit data,
  flags zero-usage subscriptions (cancel → savings) and credits about to run dry
  (top-up), runs each through the **same** guardrail, and auto-executes only what
  the guardrail ALLOWs — recording every action and the realized savings to an
  append-only ledger. One click: *agent finds and fixes waste on its own.*
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

## Tech stack

Flask · Jinja2 · Bootstrap 5 · SQLAlchemy · SQLite · Chart.js · Gunicorn ·
Nous Hermes (Nemotron-3) via an OpenAI-compatible gateway.

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
├── tests/                    # 94 tests (offline / deterministic)
├── seed.py · run.py · Dockerfile · ROADMAP.md
```

## Tests

```bash
HERMES_API_URL="" pytest -q        # 94 tests, fully offline/deterministic
```

## Docs

- `docs/GUARDRAILS.md` — the action-boundary design + tuning.
- `docs/HERMES_AUDIT.md` — the live council / tool integration.
- `ROADMAP.md` — current-state scorecard + the maturity plan.
- `DEMO_SCRIPT.md` — the 2-minute demo shot list.
