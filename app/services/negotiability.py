"""Negotiability assessment — decide IF a quote is worth negotiating, BEFORE
spending a (slow, single-threaded) model turn on a haggle.

Deterministic and offline: leverage is inferred from the vendor's price, pricing
model and the requirement spec. The autopilot uses the verdict to gate
negotiation; the UI shows it as "why we (didn't) negotiate". This is what makes
the flow honest — a one-off commodity bought at fixed list price is reported as
"not negotiable", not dressed up with a fake "annual commit" haggle.
"""
from flask import current_app


def _cfg(key, default):
    try:
        return float(current_app.config.get(key, default))
    except (RuntimeError, TypeError, ValueError):
        return float(default)


_SUB_HINTS = ("subscription", "/mo", "per month", "/month", "seat", "/year",
              "per year", "annual", "commit", "plan")
_USAGE_HINTS = ("usage", "/min", "per min", "/request", "per request", "/call",
                "per-unit usage", "metered", "/gb", "per token")
_ONETIME_HINTS = ("one-time", "one time", "per-unit", "per unit", "/unit",
                  "upfront", "delivery", "shipping")


def infer_pricing_model(vendor, spec=None):
    """Best-effort pricing model from the stored enrichment, then the
    price_basis text, then the requirement category."""
    enr = getattr(vendor, "enrichment", {}) or {}
    pm = str(enr.get("pricing_model", "")).strip().lower()
    if pm in ("subscription_monthly", "one_time", "per_unit", "usage"):
        return pm
    basis = str(getattr(vendor, "price_basis", "") or "").lower()
    if any(h in basis for h in _USAGE_HINTS):
        return "usage"
    if any(h in basis for h in _ONETIME_HINTS):
        return "one_time"
    if any(h in basis for h in _SUB_HINTS):
        return "subscription_monthly"
    cat = str((spec or {}).get("category", "")).lower()
    if cat in ("hardware", "goods", "equipment"):
        return "one_time"
    if cat in ("saas", "api", "software", "service"):
        return "subscription_monthly"
    return "unknown"


def _qty(spec):
    """Pull an integer quantity out of the free-text quantity field, else 1."""
    import re
    raw = str((spec or {}).get("quantity", "") or "")
    m = re.search(r"([\d,]+)", raw)
    if not m:
        return 1
    try:
        n = int(m.group(1).replace(",", ""))
        return max(1, n)
    except ValueError:
        return 1


def is_negotiable(vendor, spec=None):
    """Return a verdict dict::

        {negotiable, reason, leverage, tactic, pricing_model, floor_pct}

    ``floor_pct`` is the seller's hidden floor as a fraction of the quote, handed
    to the negotiation engine so a usage/commodity quote isn't pushed to an
    unrealistic discount.
    """
    spec = spec or {}
    quote = float(getattr(vendor, "price", 0) or 0)
    pm = infer_pricing_model(vendor, spec)

    recurring_min = _cfg("PROCUREMENT_NEG_RECURRING_MIN", 150)
    onetime_min = _cfg("PROCUREMENT_NEG_ONETIME_MIN", 2000)
    usage_min = _cfg("PROCUREMENT_NEG_USAGE_MIN", 1000)
    min_ticket = _cfg("PROCUREMENT_NEG_MIN_TICKET", 100)

    def no(reason):
        return {"negotiable": False, "reason": reason, "leverage": None,
                "tactic": None, "pricing_model": pm, "floor_pct": 0.0}

    if quote <= 0:
        return no("No quoted price to negotiate (free tier or quote TBD) — "
                  "request a written quote first.")

    if pm == "subscription_monthly":
        if quote < recurring_min:
            return no(f"Small recurring spend (${quote:,.0f}/mo) below the "
                      f"leverage threshold — buy at list.")
        return {"negotiable": True, "pricing_model": pm, "floor_pct": 0.82,
                "reason": f"Recurring subscription at ${quote:,.0f}/mo — vendors "
                          f"routinely discount for term commitment.",
                "leverage": "annual prepay / multi-year term",
                "tactic": "Push an annual prepay + 2-year term for ~10-18% off "
                          "list; anchor with a competing quote."}

    if pm in ("one_time", "per_unit"):
        units = _qty(spec) if pm == "per_unit" else 1
        total = quote * units
        budget = float(spec.get("budget_ceiling_usd", 0) or 0)
        big = total >= onetime_min or (budget and total >= 0.6 * budget)
        if not big:
            return no(f"One-time purchase (${total:,.0f}) at list price with "
                      f"little volume leverage — buy at quote.")
        return {"negotiable": True, "pricing_model": pm, "floor_pct": 0.85,
                "reason": f"High-value one-time purchase (${total:,.0f}) — room "
                          f"for a volume / bundle discount.",
                "leverage": "volume / bundle / competing quotes",
                "tactic": "Request a volume break and a second quote to anchor; "
                          "ask for bundled support/shipping."}

    if pm == "usage":
        if quote < usage_min:
            return no(f"Usage-metered at a low monthly estimate (${quote:,.0f}) "
                      f"— list per-unit rates rarely move at this volume.")
        return {"negotiable": True, "pricing_model": pm, "floor_pct": 0.90,
                "reason": f"High usage spend (${quote:,.0f}/mo) — committed-use "
                          f"tiers are available at this volume.",
                "leverage": "committed-use tier",
                "tactic": "Negotiate a committed-use / prepaid tier; the list "
                          "per-unit rate itself rarely moves."}

    # unknown pricing model: negotiate only if the ticket is non-trivial.
    if quote >= max(min_ticket, recurring_min):
        return {"negotiable": True, "pricing_model": pm, "floor_pct": 0.85,
                "reason": f"Quote of ${quote:,.0f} is large enough to be worth a "
                          f"conversation.",
                "leverage": "competitive quotes",
                "tactic": "Get a second quote to anchor and ask for a discount."}
    return no(f"Quote of ${quote:,.0f} is below the threshold where negotiation "
              f"is worth the effort.")
