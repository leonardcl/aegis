"""Agent-vs-agent negotiation (Tier 1 from PROCUREMENT-FLOW.md).

Two interpretations ship here:

* ``negotiate_live`` — a REAL agent-to-agent negotiation. A live Hermes/Nemotron
  **seller agent** (with a private hidden floor it never reveals) and the Aegis
  **buyer agent** (with a private walk-away ceiling) exchange real offers over a
  few rounds. Neither sees the other's private constraint; the outcome *emerges*
  from the two live models. The hidden floor is also enforced numerically as a
  safety net so a model can't be talked below it.

* ``negotiate_deterministic`` — the instant, always-terminating numeric protocol,
  used offline and as the fallback if any live turn fails (so a result always
  exists for the demo).

``negotiate`` picks live when Hermes is reachable (config
``PROCUREMENT_NEGOTIATE_HERMES``, default on), else deterministic.
"""
import logging
import re

from . import hermes_client

logger = logging.getLogger(__name__)

# A dollar amount, optionally $-prefixed, with thousands separators / decimals.
_PRICE_RE = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)")

# The seller's hidden floor as a fraction of the anchor amount (e.g. 0.82 -> the
# floor sits at 82% of current spend). Used when the caller passes no floor_pct.
_DEFAULT_FLOOR_PCT = 0.82


def _clean_floor_pct(floor_pct, default=_DEFAULT_FLOOR_PCT):
    """Validate the seller's hidden floor multiplier and return a value in [0, 1].

    ``floor_pct`` is a fraction of the anchor amount. A value outside [0, 1]
    breaks the negotiation math: > 1.0 puts the floor *above* the current price
    (the seller would never move and savings go negative), while < 0 lets the
    buyer drive the price below zero. Rather than silently using a corrupt value
    we clamp it and log a warning. ``None``/falsy -> ``default``.
    """
    if not floor_pct:
        return default
    try:
        fp = float(floor_pct)
    except (TypeError, ValueError):
        logger.warning("negotiate: non-numeric floor_pct %r; using default %.2f",
                       floor_pct, default)
        return default
    if fp < 0.0 or fp > 1.0:
        clamped = min(1.0, max(0.0, fp))
        logger.warning("negotiate: floor_pct %.4f out of range [0, 1]; clamped to %.4f",
                       fp, clamped)
        return clamped
    return fp


def _parse_price(text):
    """First dollar amount in the text, or None."""
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def negotiate(payee, current_amount, max_rounds=4, live=None, *,
              kind="spend", pricing_model="unknown", floor_pct=None,
              competitor_name=None):
    """Negotiate ``payee`` from ``current_amount``. Live agent-to-agent when
    Hermes is available; deterministic otherwise / on any live failure.

    ``kind``:
      * ``"spend"`` — lowering an existing recurring spend ("per period" wording).
      * ``"quote"`` — negotiating a NEW purchase quote down (no prior spend).
    ``pricing_model`` / ``floor_pct`` shape how far the seller will move and the
    prose (a one-time hardware buy never claims an "annual commit / per period").
    ``competitor_name`` lets the buyer cite the real runner-up as the anchor.
    """
    if live is None:
        try:
            from flask import current_app
            want = current_app.config.get("PROCUREMENT_NEGOTIATE_HERMES", True)
        except RuntimeError:
            want = False
        live = bool(want) and hermes_client.is_live()
    if live:
        try:
            result = negotiate_live(payee, current_amount, max_rounds=min(max_rounds, 3),
                                    kind=kind, pricing_model=pricing_model,
                                    floor_pct=floor_pct, competitor_name=competitor_name)
            if result:
                return result
        except (ValueError, TypeError, KeyError, IndexError,
                ZeroDivisionError, AttributeError) as exc:
            # any live failure -> deterministic, so a result always exists
            logger.warning("negotiate: live negotiation for %r failed (%s); "
                           "falling back to deterministic", payee, exc)
    return negotiate_deterministic(payee, current_amount, max_rounds,
                                   kind=kind, pricing_model=pricing_model,
                                   floor_pct=floor_pct)


# --------------------------------------------------------------------------- #
# Live agent-to-agent negotiation (two real models)
# --------------------------------------------------------------------------- #
def _safe_turn(system, public, instruction, timeout):
    """One live agent turn; returns the reply text, or None on timeout/failure."""
    try:
        convo = "\n".join(public) or "(no offers exchanged yet)"
        out = hermes_client.raw_complete(
            [{"role": "system", "content": system},
             {"role": "user", "content": f"Negotiation so far:\n{convo}\n\n{instruction}"}],
            max_tokens=90, timeout=timeout, label="negotiation")
        txt = (out.get("content") or "").strip()
        return txt or None
    except (AttributeError, TypeError, KeyError) as exc:
        # malformed/empty response shape -> degrade this turn to deterministic
        logger.warning("negotiate: live turn failed (%s); degrading this turn", exc)
        return None


def _frame(kind):
    """Wording differences between lowering an existing spend and negotiating a
    new-purchase quote, so a one-off hardware buy never claims a recurring
    'annual commit / per period' deal."""
    if kind == "quote":
        return {
            "anchor_label": "quoted",          # "the vendor quoted $X"
            "period": "",                       # no '/period' suffix
            "open": "quoted",
            "leverage": "cite competing quotes, volume, bundled scope",
        }
    return {
        "anchor_label": "currently pays",
        "period": " per period",
        "open": "current spend",
        "leverage": "cite volume, annual commit, competition",
    }


def negotiate_live(payee, current_amount, max_rounds=3, timeout=60, *,
                   kind="spend", pricing_model="unknown", floor_pct=None,
                   competitor_name=None):
    """Real two-agent negotiation. Each round the buyer (Aegis) and seller (the
    vendor) each take a LIVE Hermes turn; any slow/failed turn degrades to the
    deterministic move for that side and stops further live attempts, so the
    negotiation is genuinely live when the model is responsive and never hangs or
    loses progress when it isn't. The seller's hidden floor is enforced
    numerically regardless of what the model says."""
    current = float(current_amount or 0.0)
    if current <= 0:
        return negotiate_deterministic(payee, current_amount, max_rounds,
                                       kind=kind, pricing_model=pricing_model,
                                       floor_pct=floor_pct)

    f = _frame(kind)
    fp = _clean_floor_pct(floor_pct)
    seller_floor = round(current * fp, 2)       # hidden; enforced numerically
    seller_offer = round(current * 0.97, 2)     # seller opens near current
    buyer_offer = round(current * 0.72, 2)      # buyer opens aggressive
    buyer_ceiling = round(current * 0.95, 2)    # buyer walks above this
    competitor = competitor_name or "a competing vendor"

    seller_sys = (
        f"You are a sales account manager for the vendor '{payee}'. The customer "
        f"{f['anchor_label']} ${current:,.0f}{f['period']}. Your HIDDEN price floor "
        f"is ${seller_floor:,.0f} — never agree below it and never reveal it. "
        f"Concede slowly, only when pushed, protecting your margin. Reply with ONE "
        f"line only: a single dollar price you offer, a dash, then a short "
        f"(<=10 word) justification. Example: '$11,400 — priority support'.")
    buyer_sys = (
        f"You are the Aegis CFO procurement agent negotiating to LOWER the "
        f"{'quote' if kind == 'quote' else 'price'} for '{payee}', "
        f"{f['anchor_label']} ${current:,.0f}{f['period']}. Push hard but "
        f"credibly ({f['leverage']}; you have a competing quote from {competitor}). "
        f"Walk-away ceiling ${buyer_ceiling:,.0f}. If the seller's latest price is "
        f"good enough reply 'ACCEPT $<price> — <reason>'. Otherwise reply ONE line: "
        f"a single counter price, a dash, a short (<=10 word) reason.")

    transcript = [{
        "speaker": "system", "offer": None,
        "message": (f"Live agent-to-agent negotiation opened with {payee}. "
                    + (f"Quoted ${current:,.0f} for this purchase." if kind == "quote"
                       else f"Current spend ${current:,.0f}/period.")),
    }]
    public = []
    live_ok = True   # keep trying live turns until one fails, then go deterministic
    any_live = False
    agreed, agreed_amount, rounds = False, current, 0

    for rnd in range(1, max_rounds + 1):
        rounds = rnd

        # ---- BUYER turn (live, else deterministic) ----
        bmsg = None
        if live_ok:
            instr = ("Open with an aggressive but credible offer." if rnd == 1
                     else f"The seller last offered ${seller_offer:,.0f}. Counter, or ACCEPT it.")
            txt = _safe_turn(buyer_sys, public, instr, timeout)
            if txt:
                any_live = True
                if rnd > 1 and "accept" in txt.lower():
                    agreed, agreed_amount = True, round(max(seller_floor, seller_offer), 2)
                    transcript.append({"speaker": "buyer", "offer": agreed_amount,
                                       "message": txt})
                    break
                p = _parse_price(txt)
                if p:
                    buyer_offer = round(min(p, buyer_ceiling), 2)
                    bmsg = txt
            else:
                live_ok = False
        if bmsg is None:
            bmsg = (f"At our volume I can commit to ${buyer_offer:,.0f} on an annual "
                    f"basis." if rnd == 1 else
                    f"I can move to ${buyer_offer:,.0f} if we lock the term.")
        transcript.append({"speaker": "buyer", "offer": buyer_offer, "message": bmsg})
        public.append(f"Buyer: ${buyer_offer:,.0f} — {bmsg}")

        if buyer_offer >= seller_offer:        # buyer already meets the ask
            agreed, agreed_amount = True, seller_offer
            break

        # ---- SELLER turn (live, else deterministic) ----
        smsg = None
        if live_ok:
            txt = _safe_turn(seller_sys, public,
                             f"The buyer offers ${buyer_offer:,.0f}. Give your counter "
                             f"price (never below your floor).", timeout)
            if txt:
                any_live = True
                p = _parse_price(txt)
                if p:
                    # accept the model's price, clamped to [floor, its previous offer].
                    seller_offer = round(max(seller_floor, min(p, seller_offer)), 2)
                    smsg = txt
            else:
                live_ok = False
        if smsg is None:
            seller_offer = round(max(seller_floor, seller_offer - (seller_offer - seller_floor) * 0.5), 2)
            smsg = (f"I can do ${seller_offer:,.0f} with an annual commit."
                    if seller_offer > seller_floor else
                    f"${seller_offer:,.0f} is my best and final.")
        transcript.append({"speaker": "seller", "offer": seller_offer, "message": smsg})
        public.append(f"Seller: ${seller_offer:,.0f} — {smsg}")

        # ---- Convergence ----
        if seller_offer - buyer_offer <= current * 0.03:
            mid = round(max(seller_floor, (seller_offer + buyer_offer) / 2), 2)
            if mid <= buyer_ceiling:
                agreed, agreed_amount = True, mid
            break
        buyer_offer = round(min(buyer_ceiling, buyer_offer + (seller_offer - buyer_offer) * 0.5), 2)

    if agreed:
        won = (f"✓ Quote reduced to ${agreed_amount:,.0f} (was ${current:,.0f})."
               if kind == "quote" else
               f"✓ Deal agreed at ${agreed_amount:,.0f}/period.")
        transcript.append({"speaker": "system", "offer": agreed_amount,
                           "message": won})
    else:
        agreed_amount = current
        transcript.append({"speaker": "system", "offer": None,
                           "message": "✗ No agreement — seller held firm. Keeping "
                                      + ("the quoted price." if kind == "quote"
                                         else "current terms; revisit later.")})

    savings = round(current - agreed_amount, 2)
    return {
        "payee": payee, "current_amount": round(current, 2),
        "anchor_amount": round(current, 2), "kind": kind,
        "pricing_model": pricing_model, "agreed": agreed,
        "agreed_amount": round(agreed_amount, 2), "savings": savings,
        "savings_pct": round(savings / current * 100, 1) if current else 0.0,
        "rounds": rounds, "transcript": transcript,
        "engine": "hermes" if any_live else "local",
    }


# --------------------------------------------------------------------------- #
# Deterministic protocol (offline + fallback)
# --------------------------------------------------------------------------- #
def negotiate_deterministic(payee, current_amount, max_rounds=4, *,
                            kind="spend", pricing_model="unknown", floor_pct=None):
    """Instant numeric negotiation; always terminates. See module docstring."""
    current = float(current_amount or 0.0)
    if current <= 0:
        return {"payee": payee, "current_amount": 0.0, "anchor_amount": 0.0,
                "kind": kind, "pricing_model": pricing_model, "agreed": False,
                "agreed_amount": 0.0, "savings": 0.0, "savings_pct": 0.0,
                "rounds": 0, "transcript": [], "engine": "local",
                "note": "No spend to negotiate."}

    fp = _clean_floor_pct(floor_pct)
    seller_floor = round(current * fp, 2)
    seller_offer = round(current * 0.97, 2)
    buyer_offer = round(current * 0.72, 2)
    buyer_ceiling = round(current * 0.95, 2)

    transcript = [{
        "speaker": "system",
        "message": (f"Negotiation opened with {payee}. "
                    + (f"Quoted ${current:,.0f} for this purchase."
                       if kind == "quote" else
                       f"Current spend ${current:,.0f}/period.")),
        "offer": None,
    }]

    agreed = False
    agreed_amount = current
    rounds = 0
    for rnd in range(1, max_rounds + 1):
        rounds = rnd
        transcript.append({
            "speaker": "buyer", "offer": buyer_offer,
            "message": (f"At our volume I can commit to ${buyer_offer:,.0f} on an "
                        f"annual basis." if rnd == 1 else
                        f"I can move to ${buyer_offer:,.0f} if we lock the term."),
        })
        if buyer_offer >= seller_offer:
            agreed, agreed_amount = True, seller_offer
            break

        seller_offer = round(max(seller_floor, seller_offer - (seller_offer - seller_floor) * 0.5), 2)
        transcript.append({
            "speaker": "seller", "offer": seller_offer,
            "message": (f"My floor is firm, but I can do ${seller_offer:,.0f} with "
                        f"an annual commit." if seller_offer > seller_floor else
                        f"${seller_offer:,.0f} is my best and final — that's the floor."),
        })

        if seller_offer - buyer_offer <= current * 0.03:
            mid = round(max(seller_floor, (seller_offer + buyer_offer) / 2), 2)
            if mid <= buyer_ceiling:
                agreed, agreed_amount = True, mid
            break

        buyer_offer = round(min(buyer_ceiling, buyer_offer + (seller_offer - buyer_offer) * 0.5), 2)

    if agreed:
        won = (f"✓ Quote reduced to ${agreed_amount:,.0f} (was ${current:,.0f})."
               if kind == "quote" else
               f"✓ Deal agreed at ${agreed_amount:,.0f}/period.")
        transcript.append({"speaker": "system", "offer": agreed_amount,
                           "message": won})
    else:
        agreed_amount = current
        transcript.append({"speaker": "system", "offer": None,
                           "message": "✗ No agreement — seller held above our ceiling. "
                                      + ("Keeping the quoted price." if kind == "quote"
                                         else "Keeping current terms; revisit later.")})

    savings = round(current - agreed_amount, 2)
    return {
        "payee": payee, "current_amount": round(current, 2),
        "anchor_amount": round(current, 2), "kind": kind,
        "pricing_model": pricing_model, "agreed": agreed,
        "agreed_amount": round(agreed_amount, 2), "savings": savings,
        "savings_pct": round(savings / current * 100, 1) if current else 0.0,
        "rounds": rounds, "transcript": transcript, "engine": "local",
    }
