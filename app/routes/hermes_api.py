"""HTTP tool surface for the real Hermes sandbox.

When Hermes runs in its NemoClaw sandbox it cannot import this Flask app — it
reaches the audit engine over HTTP. These endpoints mirror hermes_tools exactly,
so the tool manifest in ``hermes/tools/aegis-audit-tools.json`` can point Hermes
straight at them.

  GET  /hermes/tools                 -> list tool specs (OpenAI function schema)
  POST /hermes/tools/<name>          -> run a tool, body: {"period_days": 30}
  POST /hermes/council/run           -> run the whole council, body: {"period_days":30}

Auth: if HERMES_TOOL_TOKEN is set in config, requests must send a matching
``Authorization: Bearer <token>`` header. Left unset for local demos.
"""
from functools import wraps

from flask import Blueprint, current_app, jsonify, request

from ..extensions import db
from ..models import ProcurementRequest
from ..services import (discovery, enrich, guardrail_service, hermes_service,
                        hermes_tools, intake, negotiation, procurement_service)

bp = Blueprint("hermes_api", __name__, url_prefix="/hermes")


def _require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = current_app.config.get("HERMES_TOOL_TOKEN", "")
        if expected:
            sent = request.headers.get("Authorization", "")
            if sent != f"Bearer {expected}":
                return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


@bp.route("/tools", methods=["GET"])
@_require_token
def list_tools():
    return jsonify({"tools": hermes_tools.TOOL_SPECS,
                    "names": hermes_tools.tool_names()})


@bp.route("/tools/<name>", methods=["POST"])
@_require_token
def call_tool(name):
    body = request.get_json(silent=True) or {}
    result = hermes_tools.run_tool(name, body)
    status = 404 if isinstance(result, dict) and result.get("error") else 200
    return jsonify(result), status


@bp.route("/council/run", methods=["POST"])
@_require_token
def run_council():
    body = request.get_json(silent=True) or {}
    try:
        period_days = int(body.get("period_days", 30) or 30)
    except (TypeError, ValueError):
        period_days = 30
    persist = bool(body.get("persist", True))
    outcome = hermes_service.run_audit_council(period_days=period_days,
                                               persist=persist)
    return jsonify({
        "report_id": outcome["report_id"],
        "engine": outcome["result"]["engine"],
        "rounds": outcome["result"]["rounds"],
        "audit": outcome["result"]["audit"],
    })


# --------------------------------------------------------------------------- #
# Procurement surface (W10) — lets the sandboxed agent drive the on-demand
# procurement flow over HTTP. Stateful: tools operate on a request_id that
# `procurement_intake` creates. Mirrors hermes/tools/aegis-procurement-tools.json.
# --------------------------------------------------------------------------- #
def _truthy(v):
    return str(v).lower() in ("1", "true", "yes", "on")


def _approval_json(a):
    if not a:
        return None
    return {"amount": a.amount, "payee": a.payee, "status": a.status,
            "decision": a.policy_decision, "rule": a.policy_rule,
            "reason": a.agent_reason}


def _vendor_json(v):
    return {
        "id": v.id, "name": v.name, "price": v.price,
        "price_basis": v.price_basis, "lead_time_days": v.lead_time_days,
        "url": v.url, "source": v.source,
        "scores": {"price": v.score_price, "time": v.score_time,
                   "risk": v.score_risk, "quality": v.score_quality,
                   "terms": v.score_terms},
        "total_score": v.total_score,
        "disqualified": v.disqualified, "disqualify_reason": v.disqualify_reason,
        "capabilities": v.capabilities,
        "negotiation": v.negotiation or None,
    }


def _request_json(r):
    return {
        "request_id": r.id, "title": r.title, "status": r.status,
        "category": r.category, "budget_ceiling": r.budget_ceiling,
        "requirement_spec": r.requirement_spec,
        "recommended_vendor_id": r.recommended_vendor_id,
        "recommended_vendor": r.recommended_vendor.name if r.recommended_vendor else None,
        "agent_recommendation": r.agent_recommendation,
        "vendors": [_vendor_json(v) for v in r.vendors],
        "approval": _approval_json(r.approval),
    }


def _load(body):
    """Return (request, None) or (None, (response, status))."""
    rid = body.get("request_id")
    if not rid:
        return None, (jsonify({"error": "request_id required"}), 400)
    r = db.session.get(ProcurementRequest, int(rid))
    if not r:
        return None, (jsonify({"error": f"request {rid} not found"}), 404)
    return r, None


@bp.route("/procurement/intake", methods=["POST"])
@_require_token
def procurement_intake():
    body = request.get_json(silent=True) or {}
    need = (body.get("need") or "").strip()
    if not need:
        return jsonify({"error": "need required"}), 400
    spec = intake.parse_need(need, use_hermes=_truthy(body.get("use_hermes")))
    req = ProcurementRequest()
    intake.apply_spec_to_request(req, spec, raw_text=need)
    db.session.add(req)
    db.session.commit()
    return jsonify(_request_json(req))


@bp.route("/procurement/discover", methods=["POST"])
@_require_token
def procurement_discover():
    body = request.get_json(silent=True) or {}
    req, err = _load(body)
    if err:
        return err
    created, resolved = discovery.discover_for_request(req, mode=body.get("mode"))
    return jsonify({"request_id": req.id, "mode": resolved,
                    "discovered": [_vendor_json(v) for v in created],
                    "vendors": [_vendor_json(v) for v in req.vendors]})


@bp.route("/procurement/enrich", methods=["POST"])
@_require_token
def procurement_enrich():
    body = request.get_json(silent=True) or {}
    req, err = _load(body)
    if err:
        return err
    summary = enrich.enrich_request(req)
    return jsonify({"request_id": req.id, "summary": summary,
                    "vendors": [_vendor_json(v) for v in req.vendors]})


@bp.route("/procurement/recommend", methods=["POST"])
@_require_token
def procurement_recommend():
    body = request.get_json(silent=True) or {}
    req, err = _load(body)
    if err:
        return err
    best = procurement_service.generate_recommendation(
        req, use_hermes=_truthy(body.get("use_hermes")))
    return jsonify({"request_id": req.id,
                    "recommended": best.name if best else None,
                    "narrative": req.agent_recommendation,
                    "vendors": [_vendor_json(v) for v in req.vendors]})


@bp.route("/procurement/negotiate", methods=["POST"])
@_require_token
def procurement_negotiate():
    body = request.get_json(silent=True) or {}
    req, err = _load(body)
    if err:
        return err
    vid = body.get("vendor_id")
    vendor = None
    if vid:
        vendor = next((v for v in req.vendors if v.id == int(vid)), None)
    vendor = vendor or req.recommended_vendor or (req.vendors[0] if req.vendors else None)
    if not vendor:
        return jsonify({"error": "no vendor to negotiate"}), 400
    result = negotiation.negotiate(vendor.name, vendor.price)
    vendor.negotiation = result
    db.session.commit()
    return jsonify({"request_id": req.id, "vendor": vendor.name, "result": result})


@bp.route("/procurement/guardrail", methods=["POST"])
@_require_token
def procurement_guardrail():
    body = request.get_json(silent=True) or {}
    req, err = _load(body)
    if err:
        return err
    if not req.vendors:
        return jsonify({"error": "no vendors to send to guardrail"}), 400
    approval = guardrail_service.send_to_guardrail(req)
    return jsonify({"request_id": req.id, "approval": _approval_json(approval)})


@bp.route("/procurement/requests/<int:rid>", methods=["GET"])
@_require_token
def procurement_get(rid):
    r = db.session.get(ProcurementRequest, rid)
    if not r:
        return jsonify({"error": f"request {rid} not found"}), 404
    return jsonify(_request_json(r))


@bp.route("/procurement/run", methods=["POST"])
@_require_token
def procurement_run():
    """One-call pipeline: intake -> discover -> enrich -> recommend ->
    (negotiate) -> guardrail. Mirrors full_audit for the procurement flow."""
    body = request.get_json(silent=True) or {}
    need = (body.get("need") or "").strip()
    if not need:
        return jsonify({"error": "need required"}), 400
    use_hermes = _truthy(body.get("use_hermes"))
    do_negotiate = body.get("negotiate", True)

    spec = intake.parse_need(need, use_hermes=use_hermes)
    req = ProcurementRequest()
    intake.apply_spec_to_request(req, spec, raw_text=need)
    db.session.add(req)
    db.session.commit()

    discovery.discover_for_request(req, mode=body.get("mode"))
    enrich.enrich_request(req)
    best = procurement_service.generate_recommendation(req, use_hermes=use_hermes)
    if best and do_negotiate:
        best.negotiation = negotiation.negotiate(best.name, best.price)
        db.session.commit()
    if req.vendors:
        guardrail_service.send_to_guardrail(req)
    return jsonify(_request_json(req))
