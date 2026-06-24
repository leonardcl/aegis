---
name: aegis-audit-council
description: >
  Run the Aegis CFO spend audit as a council of specialist personas
  (Reconciler, Compliance Officer, Period Analyst, Lead Auditor) that deliberate
  and loop to consensus. Use when an audit should be thorough and self-checking
  rather than a single pass.
version: 1.0.0
---

# Aegis CFO — Audit Council

Run the audit (`aegis-audit-flow`) as a **deliberating council**. Each persona is
a focused sub-agent with a narrow tool set; the Lead Auditor synthesises and may
loop for another round while exceptions remain unresolved.

## Personas and their tools

| Persona | Role | Tools |
|---------|------|-------|
| **Reconciler** | RECONCILE ledger ↔ Stripe | `gather_period`, `reconcile_ledger` |
| **Compliance Officer** | COMPLIANCE REPLAY vs policy in force | `compliance_replay` |
| **Period Analyst** | PERIOD REVIEW + CATEGORIZE | `period_review`, `categorize_spend` |
| **Lead Auditor** | Synthesise · REPORT · ESCALATE · decide looping | `full_audit` |

## Procedure (one round)

1. Run **Reconciler**, **Compliance Officer**, **Period Analyst** independently.
   Each calls only its own tools and returns findings with transaction-level
   detail. Do not let a persona opine outside its lane.
2. **Lead Auditor** reads all three, calls `full_audit` to confirm the
   consolidated numbers, and writes the executive summary + escalation list.

## Looping (consensus)

After the Lead's synthesis, check `full_audit().escalation_count`:

- If **0**, or this was the final allowed round → **finalise**.
- Otherwise → start another round, instructing each specialist to look closer at
  the still-unresolved transactions. Cap at **2 rounds** so the council always
  converges (no infinite deliberation).

## Why a council

- **Separation of duties** — reconciliation and compliance are judged
  independently, mirroring a real finance team. One persona can't wave through
  what another would flag.
- **Self-checking** — the Lead cross-checks specialist claims against
  `full_audit`, catching a persona that mis-stated a number.
- **Filmable** — the deliberation transcript is the on-camera proof that the
  agent polices its own books.

Reference implementation: `app/services/hermes_council.py` in the Aegis CFO repo
runs exactly this protocol against the same tools.
