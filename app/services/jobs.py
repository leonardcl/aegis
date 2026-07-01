"""Background jobs for long Hermes runs, backed by a persistent shared registry.

The audit council makes several real Hermes (Nemotron) calls and can take 1–2
minutes. Running it inside the HTTP request would hang the browser, so we run it
in a daemon thread and let the page poll for completion.

Job state is persisted in the SQLAlchemy ``Job`` table (SQLite) rather than an
in-process dict, so it is visible across all gunicorn workers and survives a
restart. The poller hits whichever worker serves the request and reads the same
durable row. Access is serialised with a module lock so concurrent
read-modify-write updates from the worker thread and the poller don't clobber
each other.

The public API (``start_audit_job`` / ``start_negotiation_job`` /
``start_autopilot_job`` / ``get_job``) is unchanged: the ``start_*`` helpers
return a job id string and ``get_job`` returns a plain dict of the job state
(same keys/shapes as before), so all callers keep working unchanged.
"""
import threading
import uuid
from datetime import datetime

from . import hermes_service
from ..extensions import db
from ..models import Job

# Serialises read-modify-write on the Job table within a process. Cross-process
# consistency is provided by SQLite itself (each job is written by one thread).
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Storage helpers (each assumes an active Flask app context for db access)
# --------------------------------------------------------------------------- #
def _persist_new(job_id, kind, state, ref=""):
    """Insert a new job row. Caller must hold ``_lock``."""
    job = Job(job_id=job_id, kind=kind, status=state.get("status", "running"),
              ref=ref or "")
    job.data = state
    db.session.add(job)
    db.session.commit()


def _update_job(job_id, **fields):
    """Merge ``fields`` into the persisted job state and commit (thread-safe)."""
    with _lock:
        job = Job.query.filter_by(job_id=job_id).first()
        if not job:
            return
        data = job.data
        data.update(fields)
        job.data = data
        if "status" in fields:
            job.status = fields["status"]
        db.session.commit()


# --------------------------------------------------------------------------- #
# Job starters
# --------------------------------------------------------------------------- #
def start_audit_job(app, period_days=30):
    """Start an audit council run in the background. Returns a job id."""
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _persist_new(job_id, "audit",
                     {"id": job_id, "kind": "audit", "status": "running",
                      "period_days": period_days, "report_id": None,
                      "engine": None, "rounds": None, "summary": None,
                      "error": None})

    def _run():
        with app.app_context():
            try:
                outcome = hermes_service.run_audit_council(period_days=period_days)
                audit = outcome["result"]["audit"]
                _update_job(job_id,
                            status="done",
                            report_id=outcome["report_id"],
                            engine=outcome["result"]["engine"],
                            rounds=outcome["result"]["rounds"],
                            summary=audit["headline"])
            except Exception as exc:  # noqa: BLE001 - surface any failure to UI
                _update_job(job_id, status="error", error=str(exc))

    threading.Thread(target=_run, daemon=True, name=f"audit-{job_id}").start()
    return job_id


def start_negotiation_job(app, vendor_id, max_rounds=3):
    """Run a (live agent-to-agent) negotiation in the background. Returns a job id.
    The vendor's negotiation result is persisted; the detail page polls for done."""
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _persist_new(job_id, "negotiation",
                     {"id": job_id, "kind": "negotiation", "status": "running",
                      "vendor_id": vendor_id, "engine": None, "agreed": None,
                      "error": None})

    def _run():
        with app.app_context():
            try:
                from ..models import VendorOption
                from . import negotiation
                vendor = db.session.get(VendorOption, vendor_id)
                result = negotiation.negotiate(vendor.name, vendor.price,
                                               max_rounds=max_rounds)
                vendor.negotiation = result
                db.session.commit()
                _update_job(job_id, status="done", engine=result.get("engine"),
                            agreed=result.get("agreed"))
            except Exception as exc:  # noqa: BLE001
                _update_job(job_id, status="error", error=str(exc))

    threading.Thread(target=_run, daemon=True, name=f"nego-{job_id}").start()
    return job_id


def start_autopilot_job(app, req_id, want_hermes=True):
    """Run the whole procurement pipeline for ``req_id`` in the background,
    streaming a staged progress log. Returns a job id. A second start for a
    request whose autopilot is still running returns the SAME job id (no
    duplicate pipeline)."""
    with _lock:
        existing = Job.query.filter_by(
            kind="autopilot", ref=str(req_id), status="running").first()
        if existing:
            return existing.job_id
        job_id = uuid.uuid4().hex[:12]
        _persist_new(job_id, "autopilot",
                     {"id": job_id, "kind": "autopilot", "status": "running",
                      "req_id": req_id, "stage": "", "events": [],
                      "recommended_vendor_id": None, "negotiated": False,
                      "error": None},
                     ref=str(req_id))

    def _log(msg, stage=None):
        with _lock:
            job = Job.query.filter_by(job_id=job_id).first()
            if not job:
                return
            data = job.data
            if stage:
                data["stage"] = stage
            data.setdefault("events", []).append(
                {"ts": datetime.utcnow().strftime("%H:%M:%S"),
                 "stage": stage or data.get("stage", ""),
                 "msg": msg})
            job.data = data
            db.session.commit()

    def _run():
        from . import autopilot
        # One app context for the whole thread so _log/_update_job can reach the
        # db; autopilot.run_autopilot pushes its own (nested) context internally.
        with app.app_context():
            try:
                outcome = autopilot.run_autopilot(app, req_id, want_hermes, _log)
                _update_job(job_id,
                            status="done",
                            recommended_vendor_id=outcome.get("recommended_vendor_id"),
                            negotiated=bool(outcome.get("negotiated")))
            except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
                _log(f"Autopilot error: {exc}", stage="error")
                _update_job(job_id, status="error", error=str(exc))

    threading.Thread(target=_run, daemon=True, name=f"autopilot-{job_id}").start()
    return job_id


def get_job(job_id):
    """Return a copy of the job state, or an empty dict if unknown."""
    with _lock:
        job = Job.query.filter_by(job_id=job_id).first()
        return dict(job.data) if job else {}
