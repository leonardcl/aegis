"""Procurement AUTOPILOT — one action, the CFO does the rest.

Given a requirement (already parsed onto a ProcurementRequest), this runs the
WHOLE pipeline end-to-end in a background thread, narrating each step into a live
progress log the UI streams:

    DISCOVER (real web search + link validation) -> ENRICH (score + fit) ->
    RECOMMEND (best-value pick) -> NEGOTIABILITY (is it even worth it?) ->
    NEGOTIATE (only if worth it) -> ready for human review.

The human reviews only at the END (approve / send to guardrail). Every stage
delegates to the existing deterministic services, so the numbers are identical to
the manual flow — autopilot only orchestrates and narrates. If discovery can't
verify any real vendor it stops honestly (``status='needs_input'``) instead of
inventing one — that is the cure for "robot arm -> Vercel".

``run_autopilot`` is the thread target; the job registry + ``log`` callback live
in ``jobs.py`` (passed in) to avoid a circular import.
"""
from ..extensions import db
from ..models import ProcurementRequest
from . import discovery, enrich, procurement_service, negotiability, negotiation


def _runner_up(req, best):
    """Best eligible vendor that isn't the winner — the credible price anchor."""
    others = [v for v in req.vendors
              if not v.disqualified and best and v.id != best.id]
    if not others:
        return None
    return max(others, key=lambda v: v.total_score or 0)


def run_autopilot(app, req_id, want_hermes, log):
    """Drive the full procurement pipeline for ``req_id``.

    ``log(msg, stage=None)`` appends a streamed progress line (and updates the
    current stage). Returns a small summary dict; also persists everything.
    """
    with app.app_context():
        req = db.session.get(ProcurementRequest, req_id)
        if not req:
            log("Request not found.", stage="error")
            return {"status": "error", "error": "request not found"}

        spec = req.requirement_spec or {}
        mode = "live" if want_hermes else "seed"

        # ---- DISCOVER -------------------------------------------------------
        log("Gathering data — reading your requirement spec.", stage="discover")
        created, resolved = discovery.discover_for_request(
            req, mode=mode,
            on_progress=lambda stage, detail="": log(detail, stage="discover"))

        if not req.vendors:
            # No verified vendor — be honest rather than confidently wrong.
            req.status = "needs_input"
            db.session.commit()
            log(f"No verified vendor found for “{req.title}”. The web search and "
                f"link checks returned nothing reliable — refine the request "
                f"(more specific item, category or budget) and rerun.",
                stage="needs_input")
            log("Done — needs your input.", stage="done")
            return {"status": "done", "recommended_vendor_id": None,
                    "needs_input": True}
        log(f"{len(req.vendors)} vendor(s) on the scorecard "
            f"(source: {resolved}).", stage="discover")

        # ---- ENRICH ---------------------------------------------------------
        log("Scoring on price, time, risk, quality and terms…", stage="enrich")
        summary = enrich.enrich_request(req)
        if summary["disqualified"]:
            for v in req.vendors:
                if v.disqualified:
                    log(f"Disqualified {v.name} — {v.disqualify_reason}.",
                        stage="enrich")
        log(f"{summary['qualified']} qualified, {summary['disqualified']} "
            f"disqualified.", stage="enrich")

        # ---- RECOMMEND ------------------------------------------------------
        log("Choosing the best-value vendor…", stage="recommend")
        best = procurement_service.generate_recommendation(
            req, use_hermes=want_hermes)
        if not best:
            req.status = "needs_input"
            db.session.commit()
            log("No eligible vendor to recommend.", stage="done")
            return {"status": "done", "recommended_vendor_id": None}
        log(f"Recommending {best.name} — score {best.total_score}/100 "
            f"at {('$%0.0f' % best.price) if best.price else 'price TBD'}.",
            stage="recommend")

        # ---- NEGOTIABILITY --------------------------------------------------
        log("Assessing whether this is worth negotiating…", stage="negotiability")
        assessment = negotiability.is_negotiable(best, spec)
        enr = best.enrichment or {}
        enr["negotiability"] = assessment
        best.enrichment = enr
        db.session.commit()

        negotiated = False
        if assessment["negotiable"]:
            log(f"Negotiable: {assessment['reason']} "
                f"Leverage: {assessment['leverage']}.", stage="negotiability")
            # ---- NEGOTIATE --------------------------------------------------
            runner = _runner_up(req, best)
            log(f"Opening negotiation with {best.name}…", stage="negotiate")
            result = negotiation.negotiate(
                best.name, best.price, kind="quote",
                pricing_model=assessment["pricing_model"],
                floor_pct=assessment["floor_pct"],
                competitor_name=(runner.name if runner else None))
            best.negotiation = result
            db.session.commit()
            negotiated = True
            if result.get("agreed"):
                log(f"Settled at ${result['agreed_amount']:,.0f} — saved "
                    f"${result['savings']:,.0f} ({result['savings_pct']}%).",
                    stage="negotiate")
            else:
                log("No deal — seller held firm; keeping the quoted price.",
                    stage="negotiate")
        else:
            log(f"Not negotiable: {assessment['reason']}", stage="negotiability")

        log("Done — ready for your review.", stage="done")
        return {"status": "done", "recommended_vendor_id": best.id,
                "negotiated": negotiated}
