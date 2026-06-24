"""Approval / guardrail queue blueprint."""
from flask import Blueprint, flash, redirect, render_template, url_for

from ..models import ApprovalRequest
from ..services.guardrail_service import decide_approval

bp = Blueprint("approvals", __name__, url_prefix="/approvals")


@bp.route("/")
def queue():
    needs_approval = (
        ApprovalRequest.query.filter_by(status="NEEDS_APPROVAL")
        .order_by(ApprovalRequest.created_at.desc())
        .all()
    )
    blocked = (
        ApprovalRequest.query.filter_by(status="BLOCKED")
        .order_by(ApprovalRequest.created_at.desc())
        .all()
    )
    decided = (
        ApprovalRequest.query.filter(
            ApprovalRequest.status.in_(["APPROVED", "REJECTED"])
        )
        .order_by(ApprovalRequest.decided_at.desc())
        .limit(15)
        .all()
    )
    return render_template(
        "approvals/queue.html",
        needs_approval=needs_approval,
        blocked=blocked,
        decided=decided,
        active_page="approvals",
    )


@bp.route("/<int:approval_id>/approve", methods=["POST"])
def approve(approval_id):
    approval = ApprovalRequest.query.get_or_404(approval_id)
    decide_approval(approval, "approve")
    flash("Request approved and posted to the ledger.", "success")
    return redirect(url_for("approvals.queue"))


@bp.route("/<int:approval_id>/reject", methods=["POST"])
def reject(approval_id):
    approval = ApprovalRequest.query.get_or_404(approval_id)
    decide_approval(approval, "reject")
    flash("Request rejected.", "warning")
    return redirect(url_for("approvals.queue"))
