# Aegis CFO — 2-minute demo shot list

Goal: show *manages money · sources spend intelligently · stays inside its mandate*.
Record real screen captures (judges can tell). Pre-run one audit before filming so
the model is warm.

**Setup**
```bash
python seed.py
HERMES_API_URL=http://localhost:8642/v1 HERMES_API_KEY=... \
  gunicorn -w 1 -b 0.0.0.0:8000 run:app      # or python run.py for :5000
```
Optional, for a public link: `AEGIS_BASIC_AUTH=demo:letmein`.

---

### 0:00–0:15 — Hook (Dashboard)
"Meet Aegis CFO. It runs a back office and spends real money — and it's the only
one you can actually trust with a card." Pan the dashboard: budget used, today's
spend, **Savings This Month**, pending approvals, blocked requests, the 7-day chart.

### 0:15–0:45 — Beat 1: autonomous, within limits (the hero loop)
Click **Run daily review**. The agent scans usage, then the flash + ledger show it
*auto-executed within policy*: cancelled two zero-usage subscriptions
(**+$2,340/mo saved**) and topped up an OpenAI credit about to run dry ($500,
auto-approved — green row). "No human touched this. It found waste and fixed it,
and it stayed inside its budget." The **Savings** KPI jumps.

### 0:45–1:15 — Beat 2: it audits its own books (the live council)
Open the chatbot, type **"run an audit of the last 30 days"** (or click **Run
Hermes Audit Council**). Open the **Audit** page and watch the five Nemotron
voices deliberate over real numbers: the Reconciler catches a **rogue $450 Stripe
charge with no ledger entry** and a **$300 amount mismatch**; Compliance passes;
the Lead escalates the two exceptions. "$29,700 spent, $9,000 saved, 100%
in-policy — and it caught spending it never authorized."

### 1:15–1:50 — Beat 3: the block (the money shot — Approvals)
Open the **Approvals** queue. Show three guardrail outcomes side by side:
- **NimbusCloud $38,500 → NEEDS_APPROVAL** (above the auto-approve limit). Approve it → posts to the ledger.
- **DataVault $72,000 → BLOCK** (over the per-transaction cap).
- **Unverified Vendor $39,000 → BLOCK** (payee not allowed). Reject it.
"The agent did all the work, but it physically cannot push these through alone —
the policy lives in a file it can't edit."

### 1:50–2:00 — Close
"Autonomous spend you can trust. Built on Hermes, Nemotron, Stripe and a
guardrail it can't override." Logo.

---

**Backup if the model is slow on camera:** the council degrades to the
deterministic local reasoner automatically — the numbers and exceptions are
identical, so the demo never stalls. Leave `HERMES_API_URL` unset to force it.
