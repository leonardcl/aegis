"""W3 ENRICH — turn raw candidates into scored, fit-checked options.

Stage 3 of the on-demand procurement flow. For each VendorOption on a request we:

  1. Check fit against the requirement's must-haves. A candidate missing a
     KNOWN must-have is **disqualified** (excluded from ranking), not merely
     down-scored — exactly as PROCUREMENT-FLOW.md specifies. We only disqualify
     when capabilities are known; a manual vendor with no capability data is left
     in (unverified) rather than wrongly killed.
  2. Compute the five criterion scores (0-100) that EVALUATE (W4) weights:
       price   — cheaper + within budget scores higher (relative across peers)
       time    — shorter lead time scores higher; misses the deadline -> penalty
       risk    — vendor reliability/track record (higher = lower risk)
       quality — reliability blended with how well it covers must/nice-haves
       terms   — contract flexibility (usage-based/month-to-month > lock-in)

All deterministic and explainable. Enrichment inputs (capabilities, reliability,
flexibility) come from discovery (seed catalogue or live), stored on the vendor's
``enrichment`` JSON; sensible defaults are used when absent.
"""
from datetime import date

from ..extensions import db
from . import procurement_service

DEFAULT_RELIABILITY = 75
DEFAULT_FLEXIBILITY = 70


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _norm(s):
    return str(s or "").strip().lower()


def _must_haves(req):
    spec = req.requirement_spec or {}
    items = spec.get("must_haves")
    if not items:
        items = [m for m in (req.must_haves or "").split(",") if m.strip()]
    return [_norm(m) for m in items if _norm(m)]


def _nice_haves(req):
    spec = req.requirement_spec or {}
    items = spec.get("nice_to_haves")
    if not items:
        items = [m for m in (req.nice_to_haves or "").split(",") if m.strip()]
    return [_norm(m) for m in items if _norm(m)]


def _covered(requirement, capabilities):
    """True if a required capability is satisfied by the vendor's capabilities."""
    r = _norm(requirement)
    return any(r == c or r in c or c in r for c in capabilities)


def _missing(requirements, capabilities):
    return [r for r in requirements if not _covered(r, capabilities)]


def _relative(value, values, lower_is_better=True, lo=40, hi=100):
    """Map ``value`` to [lo, hi] relative to peers."""
    vals = [v for v in values if v is not None]
    if len(vals) <= 1 or max(vals) == min(vals):
        return round((lo + hi) / 2)
    frac = (max(vals) - value) / (max(vals) - min(vals))  # best (lowest)=1
    if not lower_is_better:
        frac = 1 - frac
    return round(lo + (hi - lo) * frac)


def _price_score(price, prices, budget):
    if not price or price <= 0:
        return 50
    rel = _relative(price, prices, lower_is_better=True)
    if budget and budget > 0:
        if price > budget:
            return min(rel, 30)  # over budget -> heavy penalty
        headroom = (budget - price) / budget       # 0..1 under budget
        rel = max(rel, round(50 + 50 * headroom))  # ensure decent score in budget
    return max(0, min(100, rel))


def _time_score(lead, leads, req, today=None):
    today = today or date.today()
    rel = _relative(lead, leads, lower_is_better=True)
    if req.deadline:
        slack = (req.deadline - today).days
        if lead and slack is not None and lead > slack:
            rel = min(rel, 35)  # cannot make the deadline
    return max(0, min(100, rel))


def _coverage(must, nice, caps):
    wanted = must + nice
    if not wanted:
        return 1.0
    met = sum(1 for w in wanted if _covered(w, caps))
    return met / len(wanted)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def enrich_request(req, today=None):
    """Enrich + score every vendor on ``req``; mark disqualifications. Commits.

    Returns a summary dict: {enriched, disqualified, qualified}.
    """
    vendors = list(req.vendors)
    if not vendors:
        return {"enriched": 0, "disqualified": 0, "qualified": 0}

    must = _must_haves(req)
    nice = _nice_haves(req)
    prices = [v.price for v in vendors if v.price and v.price > 0]
    leads = [v.lead_time_days for v in vendors if v.lead_time_days is not None]

    disqualified = 0
    for v in vendors:
        enr = v.enrichment or {}
        caps = [_norm(c) for c in enr.get("capabilities", [])]
        reliability = int(enr.get("reliability", DEFAULT_RELIABILITY) or DEFAULT_RELIABILITY)
        flexibility = int(enr.get("flexibility", DEFAULT_FLEXIBILITY) or DEFAULT_FLEXIBILITY)

        # 1) Fit / disqualification — only when capabilities are known.
        if must and caps:
            miss = _missing(must, caps)
            if miss:
                v.disqualified = True
                v.disqualify_reason = "Missing must-have(s): " + ", ".join(miss)
            else:
                v.disqualified = False
                v.disqualify_reason = ""
        else:
            v.disqualified = False
            v.disqualify_reason = "" if caps else "Capabilities unverified"

        # 2) Criterion scores (0-100).
        cov = _coverage(must, nice, caps) if caps else 0.7
        v.score_price = _price_score(v.price, prices, req.budget_ceiling)
        v.score_time = _time_score(v.lead_time_days, leads, req, today=today)
        v.score_risk = max(0, min(100, reliability))
        v.score_quality = max(0, min(100, round(0.6 * reliability + 0.4 * 100 * cov)))
        v.score_terms = max(0, min(100, flexibility))

        if v.disqualified:
            disqualified += 1

    # Refresh weighted totals (excludes disqualified from the recommendation later).
    procurement_service.score_vendors(req)
    db.session.commit()

    return {
        "enriched": len(vendors),
        "disqualified": disqualified,
        "qualified": len(vendors) - disqualified,
    }
