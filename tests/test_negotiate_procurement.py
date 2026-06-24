"""L1 tests for W5 — negotiation wired into the procurement flow."""
from app.models import ProcurementRequest, VendorOption
from app.services import guardrail_service, negotiation


def _req_with_vendor(db, price=8000.0, name="DataVault"):
    req = ProcurementRequest(title="Warehouse", category="Cloud")
    db.session.add(req)
    db.session.commit()
    v = VendorOption(request_id=req.id, name=name, price=price)
    db.session.add(v)
    db.session.commit()
    req.recommended_vendor_id = v.id
    db.session.commit()
    return req, v


# --------------------------------------------------------------------------- #
# Persistence of the negotiation result on the vendor (W0 negotiation_json)
# --------------------------------------------------------------------------- #
def test_negotiation_persists_on_vendor(app, db):
    req, v = _req_with_vendor(db, price=10000)
    result = negotiation.negotiate(v.name, v.price)
    v.negotiation = result
    db.session.commit()
    db.session.expire_all()
    again = db.session.get(VendorOption, v.id)
    assert again.negotiation["payee"] == v.name
    assert "transcript" in again.negotiation
    assert again.negotiation["agreed_amount"] <= 10000


# --------------------------------------------------------------------------- #
# Route persists + feeds the flow
# --------------------------------------------------------------------------- #
def test_negotiate_route_populates_vendor(app, db, client):
    req, v = _req_with_vendor(db, price=9000)
    resp = client.post(f"/procurement/{req.id}/negotiate", data={"vendor_id": v.id})
    assert resp.status_code in (302, 303)
    db.session.expire_all()
    assert db.session.get(VendorOption, v.id).negotiation != {}


# --------------------------------------------------------------------------- #
# Guardrail uses the negotiated amount
# --------------------------------------------------------------------------- #
def test_guardrail_uses_negotiated_amount(app, db):
    # price 8000 -> NEEDS_APPROVAL; negotiated to 4000 -> ALLOW (< 5000 auto limit)
    req, v = _req_with_vendor(db, price=8000)
    v.negotiation = {"agreed": True, "agreed_amount": 4000.0, "savings": 4000.0,
                     "savings_pct": 50.0, "transcript": []}
    db.session.commit()
    approval = guardrail_service.send_to_guardrail(req)
    assert approval.amount == 4000.0
    assert approval.policy_decision == "ALLOW"
    assert "Negotiated to" in approval.agent_reason


def test_guardrail_uses_sticker_when_no_deal(app, db):
    req, v = _req_with_vendor(db, price=8000)
    v.negotiation = {"agreed": False, "agreed_amount": 8000.0, "savings": 0.0,
                     "transcript": []}
    db.session.commit()
    approval = guardrail_service.send_to_guardrail(req)
    assert approval.amount == 8000.0
    assert approval.policy_decision == "NEEDS_APPROVAL"
    assert "Negotiated to" not in approval.agent_reason
