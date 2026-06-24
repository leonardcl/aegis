"""Audit reporting + persistence helpers."""
from datetime import datetime

from ..extensions import db
from ..models import AuditException, AuditRecommendation, AuditReport


def latest_report():
    return AuditReport.query.order_by(AuditReport.generated_at.desc()).first()


def exception_summary(report):
    """Group a report's exceptions by type with counts."""
    summary = {}
    if not report:
        return summary
    for exc in report.exceptions:
        summary.setdefault(exc.exception_type, 0)
        summary[exc.exception_type] += 1
    return summary


def persist_council_result(result):
    """Persist a council run (from hermes_council.run_council) as an AuditReport.

    Maps the engine's structured escalations onto AuditException rows so the
    existing audit UI renders them unchanged.
    """
    audit = result["audit"]
    headline = audit["headline"]

    notes = _build_notes(result)
    report = AuditReport(
        title=f"Audit Council — last {audit['period_days']} days "
              f"({datetime.utcnow():%b %d %H:%M})",
        generated_at=datetime.utcnow(),
        total_spend=headline["total_spend"],
        total_savings=headline["projected_savings"],
        reconciliation_status=headline["reconciliation_status"],
        compliance_replay_result=headline["compliance_result"],
        notes=notes,
    )

    for esc in audit["escalations"]:
        report.exceptions.append(AuditException(
            exception_type=esc.get("exception_type", "exception"),
            description=esc.get("note") or esc.get("rule", ""),
            amount=float(esc.get("amount") or esc.get("stripe_amount")
                         or esc.get("ledger_amount") or 0.0),
            transaction_id=esc.get("transaction_id", ""),
            resolved=False,
        ))

    for rec in audit.get("advisory", {}).get("recommendations", []):
        report.advisories.append(AuditRecommendation(
            payee=rec.get("payee", ""),
            category=rec.get("category", ""),
            total=float(rec.get("total") or 0.0),
            charges=int(rec.get("charges") or 0),
            verdict=rec.get("verdict", ""),
            action=rec.get("action", ""),
            value_score=int(rec.get("value_score") or 0),
            projected_savings=float(rec.get("projected_savings") or 0.0),
            rationale=rec.get("rationale", ""),
            negotiable=bool(rec.get("negotiable")),
        ))

    db.session.add(report)
    db.session.commit()
    return report


def _build_notes(result):
    """Flatten the council transcript into a readable notes blob for the report."""
    lines = [f"Engine: {result['engine']} · rounds: {result['rounds']}", ""]
    for turn in result["transcript"]:
        tools = ", ".join(c["name"] for c in turn.get("tool_calls", [])) or "—"
        lines.append(f"[R{turn['round']}] {turn['title']} (tools: {tools})")
        if turn.get("degraded_from"):
            lines.append(f"  (fallback: {turn['degraded_from']})")
        for ln in (turn.get("content") or "").splitlines():
            lines.append(f"  {ln}")
        lines.append("")
    return "\n".join(lines).strip()
