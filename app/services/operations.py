"""Continuous autonomous operations — the CFO running on its own, on and on.

A single background daemon that, once started, repeatedly runs the autonomous
cycle on an interval, so the agent keeps managing spend with no human trigger:

    every <interval>s:  SENSE -> daily review (detect waste, act within policy,
                        record) ; and every <audit_every> cycles, convene the live
                        Hermes audit council.

Bounded and controllable on purpose: default OFF, explicit start/stop, a paced
interval (the model is single-threaded, so the per-tick work is the *deterministic*
daily review — cheap, no model contention — and the live audit runs only every Nth
cycle). Keeps a rolling cycle log + running tallies. Daemon thread: it stops on
stop() or when the process exits.
"""
import threading
from datetime import datetime

_lock = threading.Lock()
_stop = threading.Event()
_thread = None
_state = {
    "running": False, "interval": 60, "audit_every": 0,
    "cycles": 0, "actions": 0, "savings": 0.0,
    "started_at": None, "last_tick": None, "log": [],
}


def status():
    with _lock:
        s = dict(_state)
        s["log"] = list(_state["log"])
        return s


def _log(msg):
    with _lock:
        _state["log"].insert(0, {"ts": datetime.utcnow().strftime("%H:%M:%S"),
                                 "msg": msg})
        del _state["log"][40:]


def start(app, interval=60, audit_every=0):
    """Start the loop (no-op if already running). interval clamped to >= 15s."""
    global _thread
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, interval=max(15, int(interval or 60)),
                      audit_every=max(0, int(audit_every or 0)),
                      started_at=datetime.utcnow().isoformat())
    _stop.clear()
    _log(f"Continuous autonomy started — cycle every {_state['interval']}s"
         + (f", audit every {_state['audit_every']} cycles" if _state['audit_every'] else ""))
    _thread = threading.Thread(target=_loop, args=(app,), daemon=True, name="cfo-loop")
    _thread.start()
    return True


def stop():
    with _lock:
        was = _state["running"]
        _state["running"] = False
    _stop.set()
    if was:
        _log("Continuous autonomy stopped.")
    return was


def _loop(app):
    from . import autonomy, hermes_service
    while not _stop.is_set():
        try:
            with app.app_context():
                result = autonomy.run_daily_review()
                with _lock:
                    _state["cycles"] += 1
                    _state["actions"] += result["actions_taken"]
                    _state["savings"] += result["savings_month"]
                    _state["last_tick"] = datetime.utcnow().isoformat()
                    cyc, audit_every = _state["cycles"], _state["audit_every"]
                if result["actions_taken"]:
                    _log(f"Cycle {cyc}: acted on {result['actions_taken']} item(s), "
                         f"+${result['savings_month']:,.0f}/mo saved")
                else:
                    _log(f"Cycle {cyc}: reviewed — nothing new to act on (within policy)")
                if result["escalated"]:
                    _log(f"Cycle {cyc}: {len(result['escalated'])} item(s) routed to "
                         f"the human approval queue")
                if audit_every and cyc % audit_every == 0:
                    _log(f"Cycle {cyc}: convening the Hermes audit council…")
                    out = hermes_service.run_audit_council(persist=True)
                    h = out["result"]["audit"]["headline"]
                    _log(f"Cycle {cyc}: audit complete ({out['result']['engine']}) — "
                         f"${h['total_spend']:,.0f} spend, {h['exceptions']} exception(s)")
        except Exception as exc:  # noqa: BLE001 — never let one cycle kill the loop
            _log(f"Cycle error: {exc}")
        _stop.wait(max(15, _state["interval"]))
