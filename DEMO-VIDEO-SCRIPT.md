# 🎬 Aegis CFO — Competition Demo Video Script

**Target runtime:** 2 min 30 sec (safe inside the 1–3 min window)
**Tagline:** *"An autonomous CFO that thinks freely — but can't move a dollar without your guardrail."*

---

## 🧭 ALUR / FLOW (the story arc)

```
1. HOOK (problem)        → "Finance back-offices drown in manual work + risk."   [0:00–0:20]
2. MEET AEGIS (solution) → "An autonomous CFO agent." Dashboard reveal.          [0:20–0:40]
3. DEMO 1 — PROCUREMENT  → Discover vendors live → score → negotiate → guardrail [0:40–1:25]
4. DEMO 2 — AUDIT        → Council reconciles books vs Stripe, catches fraud.     [1:25–2:05]
5. THE BIG IDEA (moat)   → "Constrain actions, free cognition." Trust by design.  [2:05–2:25]
6. CLOSE / CTA           → Logo + one-line vision.                                [2:25–2:30]
```

Two live capabilities, one principle. Don't show everything — show these two well.

---

## 🎥 SHOT-BY-SHOT SCRIPT

| # | Time | ON SCREEN (visual) | VOICEOVER (narration) |
|---|------|--------------------|------------------------|
| **1** | 0:00–0:08 | Cold open: a messy spreadsheet / invoices flashing, then cut to black. Big text fades in: **"What if your CFO never slept — and never went rogue?"** | *"Every company's back office runs on the same two things: spending money… and making sure that money was spent right. Today, that's slow, manual, and risky."* |
| **2** | 0:08–0:20 | Title card: **AEGIS — The Autonomous CFO Agent**. Subtle motion. | *"Meet Aegis — an autonomous CFO agent that runs procurement, keeps the books, and audits itself."* |
| **3** | 0:20–0:38 | Screen-record: the **Dashboard** (`/`). Cursor glides over budget, spend, savings, pending approvals. | *"Here's the live back office. Aegis tracks every dollar against budget in real time — and it does the work, not just the dashboards."* |
| **4** | 0:38–0:48 | Navigate to **Procurement** (`/procurement`). Type a need, e.g. *"project management tool, $40/mo, fast onboarding."* | *"Say I need a new tool. I just describe it in plain English."* |
| **5** | 0:48–1:05 | Click **Discover** (Hermes ticked). Show vendor cards appearing with scores. *(Optional split-second highlight of a search engine icon.)* | *"Aegis searches the live web — grounded, real vendors — then scores each one on price, lead time, and fit. No hallucinated suppliers; if one search engine fails, it fails over to another automatically."* |
| **6** | 1:05–1:18 | Click **Run Autopilot** (or Negotiate on top vendor). Show the negotiation result + savings figure. | *"Then it negotiates — agent to agent — and books the best deal, here saving real money on the spot."* |
| **7** | 1:18–1:28 | The winning vendor hits the **Guardrail** → routed to the **Approval queue** (highlight "needs human approval"). | *"But here's the key: Aegis can't actually spend. Anything over the limit stops at a deterministic guardrail and waits for a human. The agent recommends — you decide."* |
| **8** | 1:28–1:40 | Navigate to **Audit** (`/audit`). Click **Run Audit Council**. Show the "deliberating…" state. | *"Now the books. Aegis convenes an audit council — five expert personas — to reconcile every transaction against Stripe, the independent source of truth."* |
| **9** | 1:40–2:00 | Report renders: highlight the **2 exceptions** — 🚨 *unknown-saas.io $450 rogue charge* and ⚠️ *CloudFlare +$300 mismatch*. Scroll the council narration. | *"And it catches what humans miss: a four-hundred-fifty-dollar charge with no paper trail, and a vendor that quietly overbilled by three hundred. Both flagged, explained, and escalated — automatically."* |
| **10** | 2:00–2:12 | Cut to a clean graphic: **"Deterministic spine + AI narration."** A diagram: numbers (locked) ← computed; words ← AI. | *"Every number is computed deterministically — the AI only explains them. So the math is always right, and you always get the why."* |
| **11** | 2:12–2:25 | Big principle on screen: **"Constrain actions. Free cognition."** | *"That's the whole philosophy: give the agent total freedom to think — and zero freedom to act without your guardrail. Autonomy you can actually trust."* |
| **12** | 2:25–2:30 | Logo + tagline + URL/handle. Fade out. | *"Aegis. Your autonomous CFO — on a leash you control."* |

---

## ⚡ 60-SECOND CUT (if the competition is strict on time)

Keep shots **1, 3, 5, 7, 9, 11**. Drop autopilot/negotiation detail and the deterministic-spine graphic.
Flow: *Problem → Dashboard → Discover vendors → Guardrail stops it → Audit catches fraud → "Constrain actions, free cognition."*

---

## 🎙️ RECORDING CHECKLIST

**Before you hit record**
- [ ] Re-seed for a clean demo: `cd /home/hermes/aegis-cfo && venv/bin/python seed.py` (gives the canonical 2 audit exceptions, no drift).
- [ ] Open the app: **http://localhost:5000** (login `cfo` / `1o-C1OJFVr1zY1vP`).
- [ ] **Pre-run one audit before filming** so the council result is cached/fast — then in the video the report appears quickly instead of waiting ~60s. (Or film the "deliberating" state and hard-cut to the finished report.)
- [ ] Have the Procurement need text pre-typed in a notepad to paste — no live typos.
- [ ] Hide bookmarks bar / use full-screen browser / clean desktop.

**Recording**
- [ ] 1080p screen capture (OBS Studio, free) at 30fps. Record system audio off, mic narration separate.
- [ ] Move the cursor slowly and deliberately; pause on the numbers you mention.
- [ ] Keep each shot short — cut on action, not on waiting.

**Editing**
- [ ] Add captions/subtitles (judges often watch muted).
- [ ] Zoom-in (Ken Burns) on the key numbers: the savings figure and the two exceptions.
- [ ] Light background music, ducked under the voiceover.
- [ ] End card holds for 3–4 seconds with project name + team.

**Tone:** confident, fast, concrete. Show real numbers on screen the moment you say them — that's what makes a demo credible to judges.

---

## 🏆 JUDGING ANGLES TO EMPHASIZE (why Aegis wins)

1. **It acts, not just chats** — discovers, scores, negotiates, audits. A real agent loop, not a chatbot.
2. **Trust by design** — the guardrail + human approval queue is the differentiator. Most "AI agents" can't safely touch money; Aegis is architected so it *can't* misbehave.
3. **Grounded, not hallucinated** — live web search with automatic failover; numbers computed deterministically.
4. **It catches real problems** — the audit literally finds a rogue charge and an overbill on camera. Tangible value.
5. **A clear principle** — *"Constrain actions, free cognition."* Memorable, and it's the thesis judges will quote.
