"""Procurement CRUD blueprint."""
from datetime import datetime

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request as flask_request,
    url_for,
)

from flask import current_app

from ..extensions import db
from ..models import ProcurementRequest, VendorOption
from ..services import discovery, enrich, intake, negotiation, procurement_service
from ..services.guardrail_service import send_to_guardrail

bp = Blueprint("procurement", __name__, url_prefix="/procurement")

STATUS_OPTIONS = [
    "draft",
    "analyzing",
    "recommended",
    "sent_to_guardrail",
    "approved",
    "rejected",
    "purchased",
]


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_money(value, default=0.0):
    """Coerce a user-entered money string to float, tolerating ``$``, thousands
    commas and ``k``/``m`` suffixes (``"$30,000"`` -> 30000.0, ``"3.5k"`` -> 3500.0).
    Never raises — bad input returns ``default`` instead of 500-ing the page."""
    if value is None:
        return default
    s = str(value).strip().lower().replace("$", "").replace(",", "").replace("_", "")
    if not s:
        return default
    mult = 1.0
    if s.endswith("k"):
        mult, s = 1_000.0, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000.0, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return default


def parse_int(value, default=0):
    """Coerce to int via :func:`parse_money`; never raises."""
    try:
        return int(round(parse_money(value, default)))
    except (ValueError, TypeError):
        return default


def _apply_form(req, form):
    req.title = form.get("title", "").strip()
    req.description = form.get("description", "").strip()
    req.category = form.get("category", "").strip()
    req.quantity_or_usage = form.get("quantity_or_usage", "").strip()
    req.deadline = _parse_date(form.get("deadline"))
    req.budget_ceiling = parse_money(form.get("budget_ceiling"), 0.0)
    req.must_haves = form.get("must_haves", "").strip()
    req.nice_to_haves = form.get("nice_to_haves", "").strip()
    req.priority_price = parse_int(form.get("priority_price"), 3)
    req.priority_time = parse_int(form.get("priority_time"), 3)
    req.priority_risk = parse_int(form.get("priority_risk"), 3)
    req.priority_quality = parse_int(form.get("priority_quality"), 3)
    req.priority_terms = parse_int(form.get("priority_terms"), 3)
    req.status = form.get("status", "draft")


@bp.route("/")
def list_requests():
    requests = ProcurementRequest.query.order_by(
        ProcurementRequest.created_at.desc()
    ).all()
    return render_template(
        "procurement/list.html", requests=requests, active_page="procurement"
    )


@bp.route("/intake", methods=["POST"])
def intake_need():
    """W1 INTAKE — parse a natural-language need into a requirement spec and
    create a pre-filled ProcurementRequest, ready for the DISCOVER stage."""
    text = flask_request.form.get("need", "").strip()
    if not text:
        flash("Describe what you need so Hermes can build a requirement spec.", "warning")
        return redirect(url_for("procurement.list_requests"))

    # Hermes-assisted parse is opt-in (slow, single-threaded model); the
    # deterministic parser is the always-on default.
    use_hermes = (
        flask_request.form.get("use_hermes") in ("1", "true", "on")
        or current_app.config.get("PROCUREMENT_INTAKE_HERMES", False)
    )
    spec = intake.parse_need(text, use_hermes=use_hermes)

    req = ProcurementRequest()
    intake.apply_spec_to_request(req, spec, raw_text=text)
    db.session.add(req)
    db.session.commit()

    flash(
        f"Requirement spec parsed ({spec.get('_engine', 'deterministic')}). "
        f"Review it, then discover vendors.",
        "success",
    )
    return redirect(url_for("procurement.detail", req_id=req.id))


@bp.route("/new", methods=["GET", "POST"])
def create():
    if flask_request.method == "POST":
        req = ProcurementRequest()
        _apply_form(req, flask_request.form)
        if not req.title:
            flash("Title is required.", "danger")
            return render_template(
                "procurement/form.html",
                req=req,
                status_options=STATUS_OPTIONS,
                active_page="procurement",
            )
        db.session.add(req)
        db.session.commit()
        flash("Procurement request created.", "success")
        return redirect(url_for("procurement.detail", req_id=req.id))

    return render_template(
        "procurement/form.html",
        req=None,
        status_options=STATUS_OPTIONS,
        active_page="procurement",
    )


@bp.route("/<int:req_id>")
def detail(req_id):
    req = ProcurementRequest.query.get_or_404(req_id)
    procurement_service.score_vendors(req)
    return render_template(
        "procurement/detail.html", req=req, active_page="procurement"
    )


@bp.route("/<int:req_id>/edit", methods=["GET", "POST"])
def edit(req_id):
    req = ProcurementRequest.query.get_or_404(req_id)
    if flask_request.method == "POST":
        _apply_form(req, flask_request.form)
        db.session.commit()
        flash("Procurement request updated.", "success")
        return redirect(url_for("procurement.detail", req_id=req.id))
    return render_template(
        "procurement/form.html",
        req=req,
        status_options=STATUS_OPTIONS,
        active_page="procurement",
    )


@bp.route("/<int:req_id>/delete", methods=["POST"])
def delete(req_id):
    req = ProcurementRequest.query.get_or_404(req_id)
    db.session.delete(req)
    db.session.commit()
    flash("Procurement request deleted.", "info")
    return redirect(url_for("procurement.list_requests"))


@bp.route("/<int:req_id>/vendors", methods=["POST"])
def add_vendor(req_id):
    req = ProcurementRequest.query.get_or_404(req_id)
    form = flask_request.form
    vendor = VendorOption(
        request_id=req.id,
        name=form.get("name", "").strip() or "Unnamed vendor",
        price=parse_money(form.get("price"), 0.0),
        lead_time_days=parse_int(form.get("lead_time_days"), 0),
        score_price=parse_int(form.get("score_price"), 0),
        score_time=parse_int(form.get("score_time"), 0),
        score_risk=parse_int(form.get("score_risk"), 0),
        score_quality=parse_int(form.get("score_quality"), 0),
        score_terms=parse_int(form.get("score_terms"), 0),
        notes=form.get("notes", "").strip(),
    )
    db.session.add(vendor)
    db.session.commit()
    flash("Vendor option added.", "success")
    return redirect(url_for("procurement.detail", req_id=req.id))


@bp.route("/<int:req_id>/discover", methods=["POST"])
def discover_vendors(req_id):
    """W2 DISCOVER — find candidate vendors for this request's requirement spec
    and add them to the scorecard."""
    req = ProcurementRequest.query.get_or_404(req_id)
    mode = flask_request.form.get("mode") or None
    # The "Hermes" checkbox asks the real agent for candidates -> live mode.
    if flask_request.form.get("use_hermes") in ("1", "true", "on"):
        mode = "live"
    created, resolved = discovery.discover_for_request(req, mode=mode)
    if created:
        if mode == "live" and resolved != "live":
            flash(
                f"Discovered {len(created)} candidate vendor(s), but Hermes was "
                "unreachable so the curated seed catalogue was used: "
                + ", ".join(v.name for v in created) + ".",
                "warning",
            )
        else:
            flash(
                f"Discovered {len(created)} candidate vendor(s) via {resolved} mode: "
                + ", ".join(v.name for v in created) + ".",
                "success",
            )
    else:
        flash("No new candidates found (they may already be on the scorecard).", "info")
    return redirect(url_for("procurement.detail", req_id=req.id))


@bp.route("/<int:req_id>/enrich", methods=["POST"])
def enrich_vendors(req_id):
    """W3 ENRICH — score every candidate and disqualify those missing a
    must-have, so the scorecard reflects real value (not just price)."""
    req = ProcurementRequest.query.get_or_404(req_id)
    if not req.vendors:
        flash("Discover or add vendors before enriching.", "warning")
        return redirect(url_for("procurement.detail", req_id=req.id))
    summary = enrich.enrich_request(req)
    flash(
        f"Enriched {summary['enriched']} vendor(s): {summary['qualified']} qualified, "
        f"{summary['disqualified']} disqualified.",
        "success",
    )
    return redirect(url_for("procurement.detail", req_id=req.id))


@bp.route("/<int:req_id>/recommend", methods=["POST"])
def recommend(req_id):
    req = ProcurementRequest.query.get_or_404(req_id)
    use_hermes = (
        flask_request.form.get("use_hermes") in ("1", "true", "on")
        or current_app.config.get("PROCUREMENT_EVAL_HERMES", False)
    )
    best = procurement_service.generate_recommendation(req, use_hermes=use_hermes)
    if best:
        engine = getattr(req, "recommendation_engine", "deterministic")
        if use_hermes and engine != "hermes":
            flash(
                f"Recommends {best.name} (deterministic — Hermes was unreachable, "
                "so the fallback narrative was used).",
                "warning",
            )
        else:
            who = "Hermes" if engine == "hermes" else "Aegis"
            flash(f"{who} recommends {best.name}.", "success")
    else:
        flash("Add vendor options before requesting a recommendation.", "warning")
    return redirect(url_for("procurement.detail", req_id=req.id))


@bp.route("/<int:req_id>/negotiate", methods=["POST"])
def negotiate_vendor(req_id):
    """W5 NEGOTIATE — run the agent-vs-agent negotiation on the recommended (or
    chosen) vendor and persist the outcome for the guardrail + ledger."""
    req = ProcurementRequest.query.get_or_404(req_id)
    vid = flask_request.form.get("vendor_id")
    vendor = None
    if vid:
        vendor = next((v for v in req.vendors if str(v.id) == str(vid)), None)
    vendor = vendor or req.recommended_vendor or (req.vendors[0] if req.vendors else None)
    if not vendor:
        flash("Recommend or add a vendor before negotiating.", "warning")
        return redirect(url_for("procurement.detail", req_id=req.id))

    result = negotiation.negotiate(vendor.name, vendor.price)
    vendor.negotiation = result
    db.session.commit()

    if result["agreed"]:
        flash(
            f"Negotiated with {vendor.name}: agreed ${result['agreed_amount']:,.0f} "
            f"(saved ${result['savings']:,.0f}, {result['savings_pct']}%).",
            "success",
        )
    else:
        flash(f"Negotiation with {vendor.name}: no agreement — keeping current terms.",
              "warning")
    return redirect(url_for("procurement.detail", req_id=req.id))


@bp.route("/<int:req_id>/send-to-guardrail", methods=["POST"])
def to_guardrail(req_id):
    req = ProcurementRequest.query.get_or_404(req_id)
    if not req.vendors:
        flash("Add at least one vendor before sending to guardrail.", "warning")
        return redirect(url_for("procurement.detail", req_id=req.id))
    approval = send_to_guardrail(req)
    flash(
        f"Sent to guardrail — policy decision: {approval.policy_decision}.",
        "info",
    )
    return redirect(url_for("approvals.queue"))
