"""SQLAlchemy models for Aegis CFO.

Relationships:
    ProcurementRequest 1--* VendorOption
    ProcurementRequest 1--1 ApprovalRequest (optional)
    ProcurementRequest 1--* LedgerEntry
    AuditReport        1--* AuditException
"""
import json
from datetime import datetime

from .extensions import db


# --------------------------------------------------------------------------- #
# Procurement
# --------------------------------------------------------------------------- #
class ProcurementRequest(db.Model):
    __tablename__ = "procurement_request"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    category = db.Column(db.String(80), default="")
    quantity_or_usage = db.Column(db.String(120), default="")
    deadline = db.Column(db.Date, nullable=True)
    budget_ceiling = db.Column(db.Float, default=0.0)

    must_haves = db.Column(db.Text, default="")
    nice_to_haves = db.Column(db.Text, default="")

    # Decision priority weights (0-5)
    priority_price = db.Column(db.Integer, default=3)
    priority_time = db.Column(db.Integer, default=3)
    priority_risk = db.Column(db.Integer, default=3)
    priority_quality = db.Column(db.Integer, default=3)
    priority_terms = db.Column(db.Integer, default=3)

    # draft, analyzing, recommended, sent_to_guardrail, approved, rejected, purchased
    status = db.Column(db.String(40), default="draft")

    agent_recommendation = db.Column(db.Text, default="")
    recommended_vendor_id = db.Column(db.Integer, nullable=True)

    # --- On-demand procurement (W0): INTAKE + BUY provenance ----------------- #
    # Original natural-language need ("I want to buy A") captured at intake.
    intake_raw = db.Column(db.Text, default="")
    # Parsed requirement spec as a JSON string (qty, deadline, budget,
    # must_haves, nice_to_haves, priority weights). Stored as text; SQLite has
    # no native JSON type. Use the ``requirement_spec`` property to read it.
    requirement_spec_json = db.Column(db.Text, default="")
    # Stripe (test-mode) subscription id created at the BUY step.
    stripe_subscription_id = db.Column(db.String(120), default="")
    purchased_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    vendors = db.relationship(
        "VendorOption",
        backref="request",
        cascade="all, delete-orphan",
        lazy=True,
        order_by="VendorOption.id",
    )
    approval = db.relationship(
        "ApprovalRequest",
        backref="request",
        uselist=False,
        cascade="all, delete-orphan",
        lazy=True,
    )
    ledger_entries = db.relationship(
        "LedgerEntry",
        backref="request",
        cascade="all, delete-orphan",
        lazy=True,
    )

    STATUS_BADGE = {
        "draft": "secondary",
        "analyzing": "info",
        "recommended": "primary",
        "sent_to_guardrail": "warning",
        "approved": "success",
        "rejected": "danger",
        "purchased": "dark",
    }

    @property
    def status_badge(self):
        return self.STATUS_BADGE.get(self.status, "secondary")

    @property
    def recommended_vendor(self):
        if not self.recommended_vendor_id:
            return None
        return next((v for v in self.vendors if v.id == self.recommended_vendor_id), None)

    @property
    def requirement_spec(self):
        """Parsed requirement spec (dict), or ``{}`` if none/invalid."""
        if not self.requirement_spec_json:
            return {}
        try:
            return json.loads(self.requirement_spec_json)
        except (ValueError, TypeError):
            return {}

    @requirement_spec.setter
    def requirement_spec(self, value):
        self.requirement_spec_json = json.dumps(value) if value else ""

    def __repr__(self):
        return f"<ProcurementRequest {self.id} {self.title!r}>"


class VendorOption(db.Model):
    __tablename__ = "vendor_option"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(
        db.Integer, db.ForeignKey("procurement_request.id"), nullable=False
    )

    name = db.Column(db.String(160), nullable=False)
    price = db.Column(db.Float, default=0.0)
    lead_time_days = db.Column(db.Integer, default=0)

    # Scorecard fields (0-100)
    score_price = db.Column(db.Integer, default=0)
    score_time = db.Column(db.Integer, default=0)
    score_risk = db.Column(db.Integer, default=0)
    score_quality = db.Column(db.Integer, default=0)
    score_terms = db.Column(db.Integer, default=0)
    total_score = db.Column(db.Float, default=0.0)

    notes = db.Column(db.Text, default="")

    # --- On-demand procurement (W0): DISCOVER / ENRICH / NEGOTIATE ---------- #
    url = db.Column(db.String(500), default="")
    # How this option entered the request: manual | discovered | seed
    source = db.Column(db.String(20), default="manual")
    # ENRICH: a candidate missing a must-have is disqualified, not down-scored.
    disqualified = db.Column(db.Boolean, default=False)
    disqualify_reason = db.Column(db.String(300), default="")
    # Human-readable basis for ``price`` (e.g. "$0.004/min @ 50k min/mo").
    price_basis = db.Column(db.String(120), default="")
    # NEGOTIATE: transcript + agreed amount + savings as a JSON string.
    # Use the ``negotiation`` property to read it.
    negotiation_json = db.Column(db.Text, default="")
    # ENRICH inputs: {capabilities: [...], reliability: int, flexibility: int}.
    # Use the ``enrichment`` property to read it.
    enrichment_json = db.Column(db.Text, default="")

    @property
    def enrichment(self):
        """Parsed enrichment inputs (dict), or ``{}`` if none/invalid."""
        if not self.enrichment_json:
            return {}
        try:
            return json.loads(self.enrichment_json)
        except (ValueError, TypeError):
            return {}

    @enrichment.setter
    def enrichment(self, value):
        self.enrichment_json = json.dumps(value) if value else ""

    @property
    def capabilities(self):
        """Convenience: the capability tag list from enrichment."""
        caps = self.enrichment.get("capabilities", [])
        return caps if isinstance(caps, list) else []

    @property
    def negotiation(self):
        """Parsed negotiation result (dict), or ``{}`` if none/invalid."""
        if not self.negotiation_json:
            return {}
        try:
            return json.loads(self.negotiation_json)
        except (ValueError, TypeError):
            return {}

    @negotiation.setter
    def negotiation(self, value):
        self.negotiation_json = json.dumps(value) if value else ""

    def __repr__(self):
        return f"<VendorOption {self.id} {self.name!r}>"


# --------------------------------------------------------------------------- #
# Approvals / Guardrail
# --------------------------------------------------------------------------- #
class ApprovalRequest(db.Model):
    __tablename__ = "approval_request"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(
        db.Integer, db.ForeignKey("procurement_request.id"), nullable=False
    )

    amount = db.Column(db.Float, default=0.0)
    payee = db.Column(db.String(160), default="")

    # NEEDS_APPROVAL, APPROVED, REJECTED, BLOCKED
    status = db.Column(db.String(40), default="NEEDS_APPROVAL")

    policy_decision = db.Column(db.String(40), default="")  # ALLOW / NEEDS_APPROVAL / BLOCK
    policy_rule = db.Column(db.String(160), default="")
    agent_reason = db.Column(db.Text, default="")

    decided_by = db.Column(db.String(120), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    decided_at = db.Column(db.DateTime, nullable=True)

    STATUS_BADGE = {
        "NEEDS_APPROVAL": "warning",
        "APPROVED": "success",
        "REJECTED": "danger",
        "BLOCKED": "danger",
    }

    DECISION_BADGE = {
        "ALLOW": "success",
        "NEEDS_APPROVAL": "warning",
        "BLOCK": "danger",
        "PENDING": "info",
    }

    @property
    def status_badge(self):
        return self.STATUS_BADGE.get(self.status, "secondary")

    @property
    def decision_badge(self):
        return self.DECISION_BADGE.get(self.policy_decision, "info")

    def __repr__(self):
        return f"<ApprovalRequest {self.id} {self.status}>"


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
class LedgerEntry(db.Model):
    __tablename__ = "ledger_entry"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(
        db.Integer, db.ForeignKey("procurement_request.id"), nullable=True
    )

    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    action = db.Column(db.String(80), default="")
    payee = db.Column(db.String(160), default="")
    amount = db.Column(db.Float, default=0.0)
    reason = db.Column(db.Text, default="")

    policy_decision = db.Column(db.String(40), default="")
    policy_rule = db.Column(db.String(160), default="")
    outcome = db.Column(db.String(80), default="")  # posted, blocked, reversed, ...
    transaction_id = db.Column(db.String(120), default="")
    created_by = db.Column(db.String(120), default="")

    OUTCOME_BADGE = {
        "posted": "success",
        "blocked": "danger",
        "reversed": "warning",
        "pending": "info",
    }

    @property
    def outcome_badge(self):
        return self.OUTCOME_BADGE.get(self.outcome, "secondary")

    def __repr__(self):
        return f"<LedgerEntry {self.id} {self.action} {self.amount}>"


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
class AuditReport(db.Model):
    __tablename__ = "audit_report"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), default="Audit Report")
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)

    total_spend = db.Column(db.Float, default=0.0)
    total_savings = db.Column(db.Float, default=0.0)

    reconciliation_status = db.Column(db.String(40), default="balanced")  # balanced / discrepancy
    compliance_replay_result = db.Column(db.String(40), default="pass")   # pass / fail

    notes = db.Column(db.Text, default="")

    exceptions = db.relationship(
        "AuditException",
        backref="report",
        cascade="all, delete-orphan",
        lazy=True,
    )
    advisories = db.relationship(
        "AuditRecommendation",
        backref="report",
        cascade="all, delete-orphan",
        lazy=True,
        order_by="AuditRecommendation.total.desc()",
    )

    RECON_BADGE = {"balanced": "success", "discrepancy": "danger"}
    REPLAY_BADGE = {"pass": "success", "fail": "danger"}

    @property
    def recon_badge(self):
        return self.RECON_BADGE.get(self.reconciliation_status, "secondary")

    @property
    def replay_badge(self):
        return self.REPLAY_BADGE.get(self.compliance_replay_result, "secondary")

    def __repr__(self):
        return f"<AuditReport {self.id}>"


class AuditException(db.Model):
    __tablename__ = "audit_exception"

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("audit_report.id"), nullable=False)

    # stripe_only_charge, ledger_only_entry, amount_mismatch, policy_violation, missing_approval
    exception_type = db.Column(db.String(60), default="")
    description = db.Column(db.Text, default="")
    amount = db.Column(db.Float, default=0.0)
    transaction_id = db.Column(db.String(120), default="")
    resolved = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"<AuditException {self.id} {self.exception_type}>"


class AuditRecommendation(db.Model):
    """Per-vendor judgment + suggested action produced by the Cost Advisor."""
    __tablename__ = "audit_recommendation"

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("audit_report.id"), nullable=False)

    payee = db.Column(db.String(160), default="")
    category = db.Column(db.String(80), default="")
    total = db.Column(db.Float, default=0.0)
    charges = db.Column(db.Integer, default=0)

    verdict = db.Column(db.String(20), default="")   # efficient / review / flagged
    action = db.Column(db.String(20), default="")    # keep / consolidate / negotiate / investigate
    value_score = db.Column(db.Integer, default=0)
    projected_savings = db.Column(db.Float, default=0.0)
    rationale = db.Column(db.Text, default="")
    negotiable = db.Column(db.Boolean, default=False)

    VERDICT_BADGE = {"efficient": "success", "review": "warning", "flagged": "danger"}
    ACTION_BADGE = {"keep": "success", "consolidate": "info",
                    "negotiate": "primary", "investigate": "danger"}

    @property
    def verdict_badge(self):
        return self.VERDICT_BADGE.get(self.verdict, "secondary")

    @property
    def action_badge(self):
        return self.ACTION_BADGE.get(self.action, "secondary")

    def __repr__(self):
        return f"<AuditRecommendation {self.id} {self.payee} {self.action}>"


# --------------------------------------------------------------------------- #
# Agent chat
# --------------------------------------------------------------------------- #
class AgentMessage(db.Model):
    __tablename__ = "agent_message"

    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(20), default="user")  # user / assistant
    content = db.Column(db.Text, default="")
    page_context = db.Column(db.String(120), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AgentMessage {self.id} {self.role}>"
