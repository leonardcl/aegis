"""Audit / ledger blueprint."""
from flask import (Blueprint, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)

from ..services import audit_service, jobs, ledger_service, negotiation

bp = Blueprint("audit", __name__, url_prefix="/audit")


@bp.route("/")
def index():
    report = audit_service.latest_report()
    entries = ledger_service.all_entries()
    exception_summary = audit_service.exception_summary(report)

    return render_template(
        "audit/index.html",
        report=report,
        entries=entries,
        exception_summary=exception_summary,
        total_spend=ledger_service.total_spend(),
        running_job=request.args.get("job", ""),
        active_page="audit",
    )


@bp.route("/run", methods=["POST"])
def run():
    """Kick off the Hermes audit council in the background and redirect back.

    The council makes several real Hermes calls (1–2 min), so we run it async and
    let the audit page poll ``/audit/status/<job>`` for completion.
    """
    try:
        period_days = int(request.form.get("period_days", 30) or 30)
    except (TypeError, ValueError):
        period_days = 30

    app = current_app._get_current_object()
    job_id = jobs.start_audit_job(app, period_days=period_days)
    flash("Hermes audit council started — deliberating now. Results will appear "
          "here automatically.", "info")
    return redirect(url_for("audit.index", job=job_id))


@bp.route("/status/<job_id>")
def status(job_id):
    """Poll endpoint for an async audit job."""
    return jsonify(jobs.get_job(job_id))


@bp.route("/negotiate", methods=["POST"])
def negotiate():
    """Start an agent-vs-agent negotiation for a flagged vendor."""
    payee = (request.form.get("payee") or "").strip()
    try:
        amount = float(request.form.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    result = negotiation.negotiate(payee, amount)
    if result["agreed"]:
        flash(f"Negotiation with {payee}: agreed ${result['agreed_amount']:,.0f} "
              f"(saved ${result['savings']:,.0f}, {result['savings_pct']}%).",
              "success")
    else:
        flash(f"Negotiation with {payee}: no agreement — keeping current terms.",
              "warning")
    return render_template("audit/negotiation.html", result=result,
                           active_page="audit")
