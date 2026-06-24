"""W4 EVALUATE — the transparent, comparative recommendation narrative.

Ranking + must-have disqualification already happen in enrich.py (scores) and
procurement_service.generate_recommendation (picks the best non-disqualified
vendor). This module produces the *explanation* — the "show your work" prose that
makes the choice trustworthy on screen:

    "<winner> over <runner-up> because <value reasons>; <others> excluded because
     they miss <must-have>."

Deterministic by default (always available, demo-safe). When enabled and the
real agent is reachable, Hermes writes the prose from the same scorecard; any
failure falls back to the deterministic narrative.
"""
import json

from flask import current_app

from . import hermes_client

# (attribute, human label) for each scored criterion.
CRITERIA = [
    ("score_price", "price"),
    ("score_time", "lead time"),
    ("score_risk", "low risk"),
    ("score_quality", "quality & fit"),
    ("score_terms", "flexible terms"),
]

_PRIORITY_LABELS = {
    "priority_price": "price",
    "priority_time": "speed",
    "priority_risk": "low risk",
    "priority_quality": "quality",
    "priority_terms": "terms",
}


def _cfg(key, default=None):
    try:
        return current_app.config.get(key, default)
    except RuntimeError:
        return default


# --------------------------------------------------------------------------- #
# Deterministic narrative
# --------------------------------------------------------------------------- #
def _top_strengths(v, n=2):
    ranked = sorted(CRITERIA, key=lambda c: getattr(v, c[0], 0), reverse=True)
    return " and ".join(f"{label} ({getattr(v, attr, 0)})" for attr, label in ranked[:n])


def _advantage_label(best, runner):
    deltas = [(label, getattr(best, attr, 0) - getattr(runner, attr, 0))
              for attr, label in CRITERIA]
    pos = max(deltas, key=lambda d: d[1])
    neg = min(deltas, key=lambda d: d[1])
    s = ""
    if pos[1] > 0:
        s += f" — ahead on {pos[0]}"
    if neg[1] <= -8:
        s += f" (though {runner.name} leads on {neg[0]})"
    return s


def _missing_items(vendor):
    reason = vendor.disqualify_reason or ""
    return reason.split(":", 1)[-1].strip() if ":" in reason else reason


def _priority_sentence(req):
    weights = {k: getattr(req, k, 3) for k in _PRIORITY_LABELS}
    top = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:2]
    labels = [_PRIORITY_LABELS[k] for k, _ in top]
    if len(labels) == 2:
        return f"Ranked by your priorities: {labels[0]} and {labels[1]} weighted highest."
    return "Ranked by your stated priorities."


def build_narrative(req, best):
    """Deterministic comparative recommendation prose."""
    vendors = list(req.vendors)
    eligible = sorted([v for v in vendors if not v.disqualified],
                      key=lambda v: v.total_score, reverse=True)
    disq = [v for v in vendors if v.disqualified]

    price_str = f"${best.price:,.0f}/mo" if best.price else "price TBD"
    basis = f" ({best.price_basis})" if best.price_basis else ""
    lead = (f"a {best.lead_time_days}-day lead time" if best.lead_time_days
            else "no setup lead")

    parts = [
        f"**{best.name}** is the best-value choice at {best.total_score}/100 — "
        f"strongest on {_top_strengths(best)}, at {price_str}{basis} with {lead}."
    ]
    runner = next((v for v in eligible if v.id != best.id), None)
    if runner:
        parts.append(
            f"It edges out **{runner.name}** ({runner.total_score}/100)"
            f"{_advantage_label(best, runner)}."
        )
    if disq:
        items = "; ".join(f"{v.name} (no {_missing_items(v)})" for v in disq)
        parts.append(f"Excluded for missing a must-have: {items}.")
    parts.append(_priority_sentence(req))
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Optional Hermes narration
# --------------------------------------------------------------------------- #
_EVAL_SYSTEM = (
    "You are a procurement analyst. Given a scorecard JSON, write a concise "
    "(3-5 sentence) recommendation in plain prose (no JSON, no markdown fences). "
    "Name the winning vendor, explain why it wins on overall value (not just "
    "price — weigh time, risk, quality, terms), contrast it with the runner-up, "
    "and state which options were disqualified and which must-have they missed. "
    "Do not invent numbers; use only what is in the scorecard."
)


def _scorecard_payload(req, best):
    return {
        "need": (req.requirement_spec or {}).get("need", req.title),
        "budget_ceiling_usd": req.budget_ceiling,
        "priorities": {label: getattr(req, attr, 3)
                       for attr, label in _PRIORITY_LABELS.items()},
        "winner": best.name,
        "options": [{
            "vendor": v.name,
            "total_score": v.total_score,
            "price": v.price,
            "price_basis": v.price_basis,
            "lead_time_days": v.lead_time_days,
            "scores": {label: getattr(v, attr, 0) for attr, label in CRITERIA},
            "disqualified": bool(v.disqualified),
            "disqualify_reason": v.disqualify_reason or "",
        } for v in req.vendors],
    }


def narrate(req, best, use_hermes=None):
    """Return (narrative_text, engine). engine is 'hermes' or 'deterministic'."""
    deterministic = build_narrative(req, best)
    if use_hermes is None:
        use_hermes = _cfg("PROCUREMENT_EVAL_HERMES", False)
    if not use_hermes or not hermes_client.is_live():
        return deterministic, "deterministic"
    try:
        result = hermes_client.chat(
            [{"role": "system", "content": _EVAL_SYSTEM},
             {"role": "user", "content": json.dumps(_scorecard_payload(req, best))}],
            use_tools=False, label="evaluate")
        if result.get("engine") == "hermes":
            text = (result.get("content") or "").strip()
            if text:
                return text, "hermes"
    except Exception:  # never let the model break the recommendation
        pass
    return deterministic, "deterministic"
