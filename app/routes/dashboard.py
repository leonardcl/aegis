"""Dashboard command center blueprint."""
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, url_for

from ..models import ApprovalRequest, AuditReport, ProcurementRequest
from ..services import audit_service, ledger_service

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    # UTC to match the utcnow()-based ledger timestamps (avoids a day-drift in
    # "today's spend" across the UTC boundary).
    today = datetime.utcnow().date()

    monthly_budget = 250_000.0
    spend = ledger_service.total_spend()
    monthly_used_pct = round(min(spend / monthly_budget * 100, 100), 1) if monthly_budget else 0

    report = audit_service.latest_report()
    # Realized savings (agent-cancelled waste) + audit projected savings.
    savings = (report.total_savings if report else 0.0) + ledger_service.total_savings()
    audit_exceptions = len(report.exceptions) if report else 0

    pending_approvals = ApprovalRequest.query.filter_by(status="NEEDS_APPROVAL").count()
    blocked_requests = ApprovalRequest.query.filter_by(status="BLOCKED").count()

    recent_ledger = ledger_service.recent_entries(limit=8)

    urgent = (
        ProcurementRequest.query.filter(
            ProcurementRequest.status.in_(
                ["analyzing", "recommended", "sent_to_guardrail"]
            )
        )
        .order_by(ProcurementRequest.deadline.asc().nullslast())
        .limit(6)
        .all()
    )

    # Guardrail status summary (counts by approval status)
    guardrail_summary = {
        "NEEDS_APPROVAL": ApprovalRequest.query.filter_by(status="NEEDS_APPROVAL").count(),
        "APPROVED": ApprovalRequest.query.filter_by(status="APPROVED").count(),
        "REJECTED": ApprovalRequest.query.filter_by(status="REJECTED").count(),
        "BLOCKED": ApprovalRequest.query.filter_by(status="BLOCKED").count(),
    }

    # 7-day spend trend for Chart.js (built from posted ledger entries)
    spend_chart = _spend_trend()

    kpis = {
        "monthly_budget": monthly_budget,
        "monthly_used": spend,
        "monthly_used_pct": monthly_used_pct,
        "today_spend": ledger_service.today_spend(today),
        "savings": savings,
        "pending_approvals": pending_approvals,
        "blocked_requests": blocked_requests,
        "audit_exceptions": audit_exceptions,
        "reconciliation_status": report.reconciliation_status if report else "—",
        "compliance_result": report.compliance_replay_result if report else "—",
    }

    return render_template(
        "dashboard.html",
        kpis=kpis,
        report=report,
        recent_ledger=recent_ledger,
        urgent=urgent,
        guardrail_summary=guardrail_summary,
        spend_chart=spend_chart,
        active_page="dashboard",
    )


@bp.route("/run-daily-review", methods=["POST"])
def run_daily_review():
    """Trigger the autonomous daily review (the P0 hero loop) and report back."""
    from ..services import autonomy
    result = autonomy.run_daily_review()
    if result["actions_taken"]:
        detail = " ".join(r["reason"] for r in result["executed"])
        flash(
            f"Daily review complete — {result['actions_taken']} action(s) "
            f"auto-executed within policy; ${result['savings_month']:,.0f}/mo saved. "
            f"{detail}",
            "success",
        )
    else:
        flash("Daily review complete — no autonomous actions were needed.", "info")
    if result["escalated"]:
        flash(f"{len(result['escalated'])} item(s) exceeded the mandate and were "
              f"routed to the approval queue.", "warning")
    if result["blocked"]:
        flash(f"{len(result['blocked'])} proposed action(s) were blocked by the "
              f"guardrail.", "danger")
    return redirect(url_for("dashboard.index"))


def _spend_trend():
    """Build a simple 7-bucket spend series for the dashboard chart."""
    from collections import OrderedDict
    from datetime import timedelta

    today = datetime.utcnow().date()
    buckets = OrderedDict()
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        buckets[d.isoformat()] = 0.0

    for entry in ledger_service.all_entries():
        if entry.outcome != "posted" or entry.timestamp is None:
            continue
        key = entry.timestamp.date().isoformat()
        if key in buckets:
            buckets[key] += entry.amount or 0.0

    return {
        "labels": [k[5:] for k in buckets.keys()],  # MM-DD
        "data": [round(v, 2) for v in buckets.values()],
    }
