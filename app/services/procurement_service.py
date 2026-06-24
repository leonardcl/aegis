"""Procurement helper logic: vendor scoring and agent recommendations."""
from ..extensions import db
from . import evaluate


def score_vendors(request):
    """Compute weighted total scores for each vendor on a request using the
    request's priority weights. Stores ``total_score`` on each vendor."""
    weights = {
        "price": request.priority_price,
        "time": request.priority_time,
        "risk": request.priority_risk,
        "quality": request.priority_quality,
        "terms": request.priority_terms,
    }
    weight_sum = sum(weights.values()) or 1

    for vendor in request.vendors:
        weighted = (
            vendor.score_price * weights["price"]
            + vendor.score_time * weights["time"]
            + vendor.score_risk * weights["risk"]
            + vendor.score_quality * weights["quality"]
            + vendor.score_terms * weights["terms"]
        )
        vendor.total_score = round(weighted / weight_sum, 1)

    return request.vendors


def generate_recommendation(request, use_hermes=None):
    """Score vendors, pick the best one, and write an agent recommendation.

    Moves the request status to ``recommended`` when a vendor is found. The
    recommendation prose is built by ``evaluate.narrate`` — a transparent
    comparative narrative, deterministic by default and Hermes-written when
    enabled (W4).
    """
    if not request.vendors:
        request.agent_recommendation = (
            "No vendor options have been added yet. Add candidates so Hermes can "
            "build a scorecard and recommend the best fit."
        )
        db.session.commit()
        return None

    score_vendors(request)
    # Disqualified vendors (missing a must-have) are excluded from selection;
    # fall back to the full set only if every option is disqualified.
    eligible = [v for v in request.vendors if not v.disqualified] or list(request.vendors)
    best = max(eligible, key=lambda v: v.total_score)

    request.recommended_vendor_id = best.id
    narrative, engine = evaluate.narrate(request, best, use_hermes=use_hermes)
    request.agent_recommendation = narrative
    # Transient (not persisted): lets the route report which engine actually ran,
    # so a silent Hermes->deterministic fallback is visible to the user.
    request.recommendation_engine = engine
    if request.status in ("draft", "analyzing"):
        request.status = "recommended"

    db.session.commit()
    return best
