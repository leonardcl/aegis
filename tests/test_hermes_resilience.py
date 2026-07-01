"""Resilience/observability regression tests for the Hermes client + config.

These run offline (conftest forces HERMES_API_URL=""); where we need the "live"
path we flip HERMES_API_URL in app.config and monkeypatch the HTTP call so no
real network is touched. They lock the contract that a bad config value, a
malformed response, or a transient timeout degrade gracefully instead of
crashing — and that transient failures are retried.
"""
import urllib.error

import pytest

from app import config as app_config
from app.services import hermes_client


@pytest.fixture(autouse=True)
def _reset_circuit():
    """Each test starts with a closed circuit breaker (module-level state)."""
    hermes_client._record_success()
    yield
    hermes_client._record_success()


# --------------------------------------------------------------------------- #
# (a) Non-numeric config coerces to the documented default instead of raising
# --------------------------------------------------------------------------- #
def test_config_env_num_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("HERMES_TIMEOUT", "not-a-number")
    assert app_config._env_num("HERMES_TIMEOUT", 90) == 90


def test_config_env_num_parses_valid(monkeypatch):
    monkeypatch.setenv("HERMES_TIMEOUT", "120")
    assert app_config._env_num("HERMES_TIMEOUT", 90) == 120


def test_client_coerce_num_falls_back(caplog):
    assert hermes_client._coerce_num("xyz", 90) == 90
    assert hermes_client._coerce_num(None, 400) == 400
    assert hermes_client._coerce_num("45", 90) == 45


def test_raw_complete_survives_non_numeric_timeout(app, monkeypatch):
    """A garbage HERMES_TIMEOUT in app.config must not raise: client coerces it
    and the call still completes (here, against a stubbed live endpoint)."""
    app.config["HERMES_API_URL"] = "http://hermes.test/v1"
    app.config["HERMES_TIMEOUT"] = "totally-bogus"

    captured = {}

    def fake_post(payload, timeout):
        captured["timeout"] = timeout
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(hermes_client, "_post_chat", fake_post)
    out = hermes_client.raw_complete([{"role": "user", "content": "hi"}])
    assert out["engine"] == "hermes"
    assert out["content"] == "ok"
    # bogus timeout was coerced to the default (90), not passed through raw.
    assert captured["timeout"] == 90


# --------------------------------------------------------------------------- #
# (b) Malformed / empty response degrades to the deterministic fallback
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [{}, {"choices": []}, {"choices": [{}]},
                                 {"choices": "nope"}, [], None,
                                 {"choices": [{"message": "x"}]}])
def test_message_from_response_rejects_malformed(bad):
    with pytest.raises(ValueError):
        hermes_client._message_from_response(bad)


def test_message_from_response_accepts_valid():
    msg = hermes_client._message_from_response(
        {"choices": [{"message": {"content": "hello"}}]})
    assert msg["content"] == "hello"


@pytest.mark.parametrize("bad", [{}, {"choices": []}])
def test_raw_complete_malformed_falls_back(app, monkeypatch, bad):
    app.config["HERMES_API_URL"] = "http://hermes.test/v1"
    monkeypatch.setattr(hermes_client, "_post_chat", lambda p, t: bad)
    out = hermes_client.raw_complete([{"role": "user", "content": "hi"}])
    assert out["engine"] == "local"
    assert out["content"] is None
    assert "hermes unreachable" in out["degraded_from"]


@pytest.mark.parametrize("bad", [{}, {"choices": []}])
def test_chat_malformed_falls_back_to_local_narrator(app, monkeypatch, bad):
    app.config["HERMES_API_URL"] = "http://hermes.test/v1"
    monkeypatch.setattr(hermes_client, "_post_chat", lambda p, t: bad)
    out = hermes_client.chat(
        [{"role": "user", "content": "reconcile the stripe ledger"}])
    # Deterministic narrator path: engine local, real content, marked degraded.
    assert out["engine"] == "local"
    assert out["content"]  # non-empty narrative from the local reasoner
    assert "hermes unreachable" in out.get("degraded_from", "")


# --------------------------------------------------------------------------- #
# (c) Retry on a simulated timeout, then succeed
# --------------------------------------------------------------------------- #
def test_post_chat_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(hermes_client.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky(payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("simulated read timeout")
        return {"choices": [{"message": {"content": "recovered"}}]}

    monkeypatch.setattr(hermes_client, "_post_chat", flaky)
    resp = hermes_client._post_chat_resilient({"model": "m"}, 10, "test")
    assert calls["n"] == 2  # one failure, one success
    assert resp["choices"][0]["message"]["content"] == "recovered"


def test_raw_complete_retries_then_succeeds(app, monkeypatch):
    app.config["HERMES_API_URL"] = "http://hermes.test/v1"
    monkeypatch.setattr(hermes_client.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky(payload, timeout):
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.URLError("connection refused")
        return {"choices": [{"message": {"content": "live answer"}}]}

    monkeypatch.setattr(hermes_client, "_post_chat", flaky)
    out = hermes_client.raw_complete([{"role": "user", "content": "hi"}])
    assert out["engine"] == "hermes"
    assert out["content"] == "live answer"
    assert calls["n"] == 2


def test_post_chat_does_not_retry_4xx(monkeypatch):
    monkeypatch.setattr(hermes_client.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def client_error(payload, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://x", 400, "Bad Request", {}, None)

    monkeypatch.setattr(hermes_client, "_post_chat", client_error)
    with pytest.raises(urllib.error.HTTPError):
        hermes_client._post_chat_resilient({"model": "m"}, 10, "test")
    assert calls["n"] == 1  # 4xx is not retried


def test_post_chat_exhausts_retries_on_persistent_failure(monkeypatch):
    monkeypatch.setattr(hermes_client.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def always_timeout(payload, timeout):
        calls["n"] += 1
        raise TimeoutError("down")

    monkeypatch.setattr(hermes_client, "_post_chat", always_timeout)
    with pytest.raises(TimeoutError):
        hermes_client._post_chat_resilient({"model": "m"}, 10, "test")
    assert calls["n"] == hermes_client.MAX_ATTEMPTS


# --------------------------------------------------------------------------- #
# Circuit breaker: after N consecutive failures it short-circuits the fallback
# --------------------------------------------------------------------------- #
def test_circuit_breaker_opens_and_short_circuits(monkeypatch):
    monkeypatch.setattr(hermes_client.time, "sleep", lambda *_: None)

    def always_timeout(payload, timeout):
        raise TimeoutError("down")

    monkeypatch.setattr(hermes_client, "_post_chat", always_timeout)

    # Each resilient call = one "consecutive failure" toward the threshold.
    for _ in range(hermes_client.CIRCUIT_THRESHOLD):
        with pytest.raises(TimeoutError):
            hermes_client._post_chat_resilient({"model": "m"}, 10, "test")

    assert hermes_client._circuit_open()

    # With the breaker open the next call short-circuits without touching HTTP.
    calls = {"n": 0}

    def spy(payload, timeout):
        calls["n"] += 1
        raise TimeoutError("down")

    monkeypatch.setattr(hermes_client, "_post_chat", spy)
    with pytest.raises(hermes_client.HermesUnavailable):
        hermes_client._post_chat_resilient({"model": "m"}, 10, "test")
    assert calls["n"] == 0  # endpoint was never hit while the circuit is open
