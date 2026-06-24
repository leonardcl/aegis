"""Agent-vs-agent negotiation (Tier 1 from PROCUREMENT-FLOW.md).

A buyer agent (Hermes / Aegis CFO) negotiates against a mock **seller agent** that
has a hidden price floor and a simple concession policy. They exchange offers over
a few rounds until they agree or walk away. Nothing is scripted to a fixed price —
the outcome emerges from the two strategies, so it is an honest negotiation in a
controlled sandbox.

The numeric protocol is deterministic (always terminates, instant, demoable). When
the real Hermes is connected it can additionally narrate the buyer's closing
rationale, but the offers themselves are computed here so a result always exists.
"""


def negotiate(payee, current_amount, max_rounds=4):
    """Run a negotiation for ``payee`` starting from ``current_amount``.

    Returns:
        dict: {payee, current_amount, agreed, agreed_amount, savings,
               savings_pct, rounds, transcript[]}
    """
    current = float(current_amount or 0.0)
    if current <= 0:
        return {"payee": payee, "current_amount": 0.0, "agreed": False,
                "agreed_amount": 0.0, "savings": 0.0, "savings_pct": 0.0,
                "rounds": 0, "transcript": [],
                "note": "No spend to negotiate."}

    # Seller's hidden floor and opening; buyer's opening and walk-away ceiling.
    seller_floor = round(current * 0.82, 2)      # seller won't go below this
    seller_offer = round(current * 0.97, 2)      # seller opens near current
    buyer_offer = round(current * 0.72, 2)       # buyer opens aggressive
    buyer_ceiling = round(current * 0.95, 2)     # buyer walks if forced above

    transcript = [{
        "speaker": "system",
        "message": (f"Negotiation opened with {payee}. Current spend "
                    f"${current:,.0f}/period."),
        "offer": None,
    }]

    agreed = False
    agreed_amount = current
    rounds = 0
    for rnd in range(1, max_rounds + 1):
        rounds = rnd
        # Buyer proposes.
        transcript.append({
            "speaker": "buyer", "offer": buyer_offer,
            "message": (f"At our volume I can commit to ${buyer_offer:,.0f} on an "
                        f"annual basis." if rnd == 1 else
                        f"I can move to ${buyer_offer:,.0f} if we lock the term."),
        })
        # Deal if buyer's offer already clears the seller's floor.
        if buyer_offer >= seller_offer:
            agreed, agreed_amount = True, seller_offer
            break

        # Seller concedes toward (but not below) its floor.
        seller_offer = round(max(seller_floor, seller_offer - (seller_offer - seller_floor) * 0.5), 2)
        transcript.append({
            "speaker": "seller", "offer": seller_offer,
            "message": (f"My floor is firm, but I can do ${seller_offer:,.0f} with "
                        f"an annual commit." if seller_offer > seller_floor else
                        f"${seller_offer:,.0f} is my best and final — that's the floor."),
        })

        # Agreement test: offers within 3% of each other → meet in the middle.
        if seller_offer - buyer_offer <= current * 0.03:
            mid = round(max(seller_floor, (seller_offer + buyer_offer) / 2), 2)
            if mid <= buyer_ceiling:
                agreed, agreed_amount = True, mid
            break

        # Buyer concedes upward for the next round.
        buyer_offer = round(min(buyer_ceiling, buyer_offer + (seller_offer - buyer_offer) * 0.5), 2)

    if agreed:
        transcript.append({
            "speaker": "system", "offer": agreed_amount,
            "message": f"✓ Deal agreed at ${agreed_amount:,.0f}/period.",
        })
    else:
        agreed_amount = current
        transcript.append({
            "speaker": "system", "offer": None,
            "message": "✗ No agreement — seller held above our ceiling. Keeping "
                       "current terms; revisit later.",
        })

    savings = round(current - agreed_amount, 2)
    return {
        "payee": payee,
        "current_amount": round(current, 2),
        "agreed": agreed,
        "agreed_amount": round(agreed_amount, 2),
        "savings": savings,
        "savings_pct": round(savings / current * 100, 1) if current else 0.0,
        "rounds": rounds,
        "transcript": transcript,
    }
