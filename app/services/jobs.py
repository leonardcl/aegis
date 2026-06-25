"""Lightweight in-process background jobs for long Hermes runs.

The audit council makes several real Hermes (Nemotron) calls and can take 1–2
minutes. Running it inside the HTTP request would hang the browser, so we run it
in a daemon thread and let the page poll for completion.

This is an in-memory registry (fine for a single-host demo). For multi-process
gunicorn, jobs are visible only to the worker that started them — the audit page
polls the same worker via the job id; the persisted AuditReport is the durable
result either way.
"""
import threading
import uuid

from . import hermes_service

_jobs = {}
_lock = threading.Lock()


def start_audit_job(app, period_days=30):
    """Start an audit council run in the background. Returns a job id."""
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {"id": job_id, "kind": "audit", "status": "running",
                         "period_days": period_days, "report_id": None,
                         "engine": None, "rounds": None, "summary": None,
                         "error": None}

    def _run():
        with app.app_context():
            try:
                outcome = hermes_service.run_audit_council(period_days=period_days)
                audit = outcome["result"]["audit"]
                with _lock:
                    _jobs[job_id].update(
                        status="done",
                        report_id=outcome["report_id"],
                        engine=outcome["result"]["engine"],
                        rounds=outcome["result"]["rounds"],
                        summary=audit["headline"],
                    )
            except Exception as exc:  # noqa: BLE001 - surface any failure to UI
                with _lock:
                    _jobs[job_id].update(status="error", error=str(exc))

    threading.Thread(target=_run, daemon=True, name=f"audit-{job_id}").start()
    return job_id


def start_negotiation_job(app, vendor_id, max_rounds=3):
    """Run a (live agent-to-agent) negotiation in the background. Returns a job id.
    The vendor's negotiation result is persisted; the detail page polls for done."""
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {"id": job_id, "kind": "negotiation", "status": "running",
                         "vendor_id": vendor_id, "engine": None, "agreed": None,
                         "error": None}

    def _run():
        with app.app_context():
            try:
                from ..extensions import db
                from ..models import VendorOption
                from . import negotiation
                vendor = db.session.get(VendorOption, vendor_id)
                result = negotiation.negotiate(vendor.name, vendor.price,
                                               max_rounds=max_rounds)
                vendor.negotiation = result
                db.session.commit()
                with _lock:
                    _jobs[job_id].update(status="done", engine=result.get("engine"),
                                         agreed=result.get("agreed"))
            except Exception as exc:  # noqa: BLE001
                with _lock:
                    _jobs[job_id].update(status="error", error=str(exc))

    threading.Thread(target=_run, daemon=True, name=f"nego-{job_id}").start()
    return job_id


def get_job(job_id):
    """Return a copy of the job state, or an empty dict if unknown."""
    with _lock:
        return dict(_jobs.get(job_id) or {})
