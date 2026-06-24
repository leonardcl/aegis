"""L3 tests for W10 — the procurement HTTP surface the sandbox agent drives."""


def test_run_pipeline_one_call(app, client):
    resp = client.post("/hermes/procurement/run", json={
        "need": "I need a transcription API under $300/mo with English and "
                "Indonesian and speaker diarization, ~50,000 min/month",
        "negotiate": True,
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["recommended_vendor"] == "Deepgram"
    # two disqualified (AssemblyAI: indonesian, Whisper: diarization)
    disq = [v for v in data["vendors"] if v["disqualified"]]
    assert len(disq) == 2
    # negotiation happened on the recommended vendor
    rec = next(v for v in data["vendors"] if v["name"] == "Deepgram")
    assert rec["negotiation"] is not None
    # guardrail ran
    assert data["approval"]["decision"] in ("ALLOW", "NEEDS_APPROVAL", "BLOCK")


def test_step_by_step_flow(app, client):
    r = client.post("/hermes/procurement/intake",
                    json={"need": "transcription API under $300/mo with english and indonesian and speaker diarization"})
    rid = r.get_json()["request_id"]
    assert r.get_json()["requirement_spec"]["budget_ceiling_usd"] == 300

    d = client.post("/hermes/procurement/discover", json={"request_id": rid})
    assert len(d.get_json()["discovered"]) >= 3

    e = client.post("/hermes/procurement/enrich", json={"request_id": rid})
    assert e.get_json()["summary"]["disqualified"] == 2

    rec = client.post("/hermes/procurement/recommend", json={"request_id": rid})
    assert rec.get_json()["recommended"] == "Deepgram"

    n = client.post("/hermes/procurement/negotiate", json={"request_id": rid})
    assert n.get_json()["result"]["agreed_amount"] <= 215

    g = client.post("/hermes/procurement/guardrail", json={"request_id": rid})
    assert "decision" in g.get_json()["approval"]

    state = client.get(f"/hermes/procurement/requests/{rid}")
    assert state.get_json()["recommended_vendor"] == "Deepgram"


def test_intake_requires_need(app, client):
    assert client.post("/hermes/procurement/intake", json={}).status_code == 400


def test_load_missing_request_404(app, client):
    assert client.post("/hermes/procurement/enrich",
                       json={"request_id": 99999}).status_code == 404


def test_token_guard(app, client):
    app.config["HERMES_TOOL_TOKEN"] = "secret"
    # missing token -> 401
    assert client.post("/hermes/procurement/intake",
                       json={"need": "x"}).status_code == 401
    # correct token -> ok
    ok = client.post("/hermes/procurement/intake", json={"need": "a crm tool"},
                     headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200
