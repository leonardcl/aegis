# Hermes Audit Capability — How it works & how to operate it

This document describes the AUDIT-FLOW capability added to Aegis CFO and how to
connect it to the **real** Hermes Agent (the Nous-Research agent running in the
NemoClaw sandbox on Nemotron-3-Super-120B).

It implements `_TARGET_TODO/AUDIT-FLOW.md` with four properties you asked for:
**tool calling**, **looping**, an internal **council**, and the full audit flow.

---

## 1. Architecture at a glance

```
                         Aegis CFO (Flask)
  ┌──────────────────────────────────────────────────────────────┐
  │ routes/audit.py  POST /audit/run                              │
  │      │                                                        │
  │      ▼                                                        │
  │ hermes_service.run_audit_council()                           │
  │      │                                                        │
  │      ▼                                                        │
  │ hermes_council.run_council()   ← the COUNCIL (loops to        │
  │   ├─ Reconciler ─┐              consensus, max 2 rounds)      │
  │   ├─ Compliance ─┤  each persona = hermes_client.chat(...)   │
  │   ├─ Period ─────┤            (tool-calling loop)            │
  │   └─ Lead ───────┘                                            │
  │      │                 calls tools ▼                          │
  │      │             hermes_tools.run_tool()                    │
  │      │                       │                                │
  │      │                       ▼                                │
  │      │             audit_engine (deterministic 7 steps)       │
  │      │               reads: LedgerEntry + stripe_source       │
  │      ▼                                                        │
  │ audit_service.persist_council_result() → AuditReport + rows  │
  └──────────────────────────────────────────────────────────────┘
            ▲ (alt path) the REAL Hermes sandbox calls the SAME
            │ engine over HTTP:  POST /hermes/tools/<name>
```

Two ways the LLM reasoning happens:

- **Local reasoner** (default, `HERMES_API_URL` empty): a deterministic stand-in
  drives the tools and narrates from real engine output. The demo always works
  offline; numbers are always real.
- **Real Hermes** (`HERMES_API_URL` set): `hermes_client` calls Hermes'
  OpenAI-compatible Chat Completions endpoint with the tool specs and runs a real
  tool-calling loop. Nemotron decides which tools to call and writes the prose.

The switch is **config only** — no code changes.

---

## 2. Files added

| File | Role |
|------|------|
| `app/services/stripe_source.py` | Mock Stripe "source of truth" (planted rogue charge / mismatch). |
| `app/services/audit_engine.py` | Deterministic 7-step flow: gather, reconcile, compliance_replay, period_review, categorize, full_audit. |
| `app/services/hermes_tools.py` | OpenAI tool specs + dispatch over the engine. |
| `app/services/hermes_client.py` | OpenAI-compatible client + tool loop + local fallback. |
| `app/services/hermes_council.py` | The 4-persona council with looping. |
| `app/services/hermes_service.py` | Bridge: `ask_hermes_agent`, `run_audit_council`. |
| `app/services/audit_service.py` | `persist_council_result` + report helpers. |
| `app/routes/audit.py` | `POST /audit/run`. |
| `app/routes/hermes_api.py` | `/hermes/tools/*` + `/hermes/council/run` HTTP surface for the sandbox. |
| `hermes/skills/aegis-audit-flow/SKILL.md` | Skill: the audit procedure for the real agent. |
| `hermes/skills/aegis-audit-council/SKILL.md` | Skill: the council protocol. |
| `hermes/tools/aegis-audit-tools.json` | Tool manifest pointing the sandbox at the HTTP surface. |

---

## 3. Running it (local, no sandbox)

```bash
cd aegis-cfo
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python seed.py
python run.py            # http://localhost:5000
```

Open **Audit & Ledger** → click **Run Hermes Audit Council**. You'll see:

- Reconciliation flips to **discrepancy** (the planted `txn_str_555` rogue charge
  to `unknown-saas.io` and the CloudFlare amount mismatch are caught).
- Compliance replay result + any missing-approval breach (the AWS charge).
- The **council deliberation transcript** at the bottom of the page.
- New `AuditException` rows in the exceptions panel.

CLI smoke test without the web UI:

```bash
python -c "
from app import create_app
from app.services import hermes_service
app = create_app()
with app.app_context():
    out = hermes_service.run_audit_council(30)
    print('engine:', out['result']['engine'], 'report_id:', out['report_id'])
    print('headline:', out['result']['audit']['headline'])
"
```

---

## 4. Connecting the REAL Hermes Agent

The Hermes sandbox exposes an OpenAI-compatible API on **port 8642** (`/v1`,
bearer auth), model `nvidia/nemotron-3-super-120b-a12b` via the NemoClaw gateway
(confirmed in `~/.nemoclaw/source/agents/hermes/manifest.yaml`).

### Option A — Aegis CFO drives Hermes (recommended)

Aegis CFO calls Hermes; Hermes calls the audit tools back. Set in `.env`:

```bash
HERMES_API_URL=http://localhost:8642/v1
HERMES_API_KEY=<bearer token from the sandbox>
HERMES_MODEL=nvidia/nemotron-3-super-120b-a12b
```

Now `hermes_client.is_live()` is true and each council persona becomes a real
Nemotron call with the audit tools attached. The local reasoner remains the
automatic fallback if the sandbox is unreachable (degraded mode is reported in
the transcript).

> Tool execution still happens **inside Aegis CFO** (the client runs the tool
> loop locally and feeds results back to Hermes). This keeps DB access in-process
> and is the simplest wiring.

### Option B — Hermes drives the tools over HTTP

Let the agent inside the sandbox own the loop and reach back to Aegis CFO:

1. Make Aegis CFO reachable from the sandbox (host gateway IP, `host.docker.internal`,
   or a tunnel) and optionally set `HERMES_TOOL_TOKEN` to protect it.
2. Install the skills + manifest into the sandbox config dir (`/sandbox/.hermes`):
   - copy `hermes/skills/*` into the agent's skills directory,
   - copy `hermes/tools/aegis-audit-tools.json`, set `base_url` to the reachable
     Aegis CFO `/hermes` URL and `AEGIS_TOOL_TOKEN` to match `HERMES_TOOL_TOKEN`.
3. Reload skills (the NemoClaw plugin exposes `nemoclaw_reload_skills`).
4. Ask Hermes: *"Run the Aegis audit council for the last 30 days."*

The sandbox then calls `POST /hermes/tools/<name>` for each step and (optionally)
`POST /hermes/council/run` to persist a report.

### Running the server (important for async audits)

The audit council runs in a background thread and the page polls for it. The job
registry is in-memory, so run gunicorn with **one worker and multiple threads**
(shared memory, still concurrent for the I/O-bound Hermes calls):

```bash
set -a; . ./.env; set +a
gunicorn "run:app" --bind 0.0.0.0:5000 --workers 1 --threads 8 --timeout 300
```

With multiple workers the poll can hit a different worker than the one running
the job (status would read empty); the persisted report still appears on reload.

### Verify the endpoint

```bash
curl -s http://localhost:8642/health           # -> {"status":"ok",...}
curl -s -X POST http://localhost:5000/hermes/tools/reconcile_ledger \
     -H 'Content-Type: application/json' -d '{"period_days":30}' | head
```

---

## 5. The four capabilities, mapped

| You asked for | Where it lives |
|---------------|----------------|
| **Tool calling** | `hermes_tools.TOOL_SPECS` (OpenAI schema) + `run_tool`; live loop in `hermes_client._chat_live`; HTTP surface in `routes/hermes_api.py`. |
| **Looping** | per-persona tool loop (`MAX_TOOL_ROUNDS`); council consensus loop (`hermes_council.run_council`, `MAX_ROUNDS`). |
| **Council** | `hermes_council` — Reconciler · Compliance · Period · Lead, separation of duties + Lead cross-check. |
| **AUDIT-FLOW** | `audit_engine` implements GATHER→RECONCILE→COMPLIANCE→PERIOD→CATEGORIZE→REPORT→ESCALATE verbatim. |

---

## 5b. Advisory + negotiation (proactive, beyond exceptions)

The audit doesn't stop at flagging exceptions — it **judges every vendor** and can
**act on the judgment**:

- **Advisory** (`audit_engine.recommendations`): each vendor gets a verdict
  (`efficient`/`review`/`flagged`), a suggested action (`keep`/`consolidate`/
  `negotiate`/`investigate`), a value score, a rationale and projected savings —
  even for healthy spend. Surfaced by the **Cost Advisor** council persona and a
  Vendor Judgments table on the audit page.
- **Negotiation** (`negotiation.negotiate`): the Tier-1 agent-vs-agent flow from
  PROCUREMENT-FLOW.md. For any `negotiate` item, the **⇄ Negotiate** button runs
  Hermes (buyer) against a mock seller agent with a hidden floor; offers are
  exchanged over a few rounds until they agree or walk away. The result page
  shows the transcript, the agreed price and the saving. An agreed deal would
  then route through the guardrail for human approval before the plan switches.

## 6. Notes & honest limitations

- **Stripe is mocked** (`stripe_source.py`). For real reconciliation, swap its
  `get_charges()` body for a Stripe (test-mode) API call returning the same shape.
- **Point-in-time policy**: `compliance_replay` reads a single `POLICY` snapshot.
  For true historical replay, version the policy and stamp each `LedgerEntry`
  with the version in force (see the analysis note in the project docs).
- **Savings** is labelled *projected* (guardrail-blocked spend), not realised.
- The local reasoner is a deterministic stand-in, **not** an LLM — it exists so
  the dashboard is always demoable. Real reasoning requires Option A or B above.
