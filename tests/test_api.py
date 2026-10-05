"""HTTP API end-to-end: listing, snippets, decisions, auth, SSE, ops."""

from __future__ import annotations

import json


def _pending(client) -> dict:
    reqs = client.get("/api/requests").json()["requests"]
    pending = [r for r in reqs if r["state"] == "pending"]
    assert pending, "expected pending demo requests"
    return pending[0]


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] and body["adapter"] == "demo"


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "nemodeck" in r.text


def test_overview(client):
    ov = client.get("/api/overview").json()
    assert ov["counts"]["sandboxes"] == 2
    assert ov["counts"]["running"] == 1
    names = {s["name"] for s in ov["sandboxes"]}
    assert names == {"spark-hermes", "oc-lab"}


def test_requests_and_snippet(client):
    req = _pending(client)
    snip = client.get(f"/api/requests/{req['id']}/snippet").json()["yaml"]
    assert "preset:" in snip and req["host"] in snip


def test_full_approval_roundtrip(client):
    req = _pending(client)
    snip = client.get(f"/api/requests/{req['id']}/snippet").json()["yaml"]
    res = client.post(f"/api/requests/{req['id']}/approve", json={"note": "ok for task", "yaml": snip}).json()
    assert res["result"]["ok"], res["result"]["output"]
    assert res["request"]["state"] == "approved"

    after = client.get("/api/requests").json()["requests"]
    match = [r for r in after if r["id"] == req["id"]][0]
    assert match["state"] == "approved" and match["approved_as"]

    audit = client.get("/api/audit", params={"tail": 50}).json()["entries"]
    assert any(e["action"] == "approve_request" and e["target"] == f"{req['host']}:{req['port']}" for e in audit)


def test_approve_rejects_malformed_preset(client):
    req = _pending(client)
    r = client.post(f"/api/requests/{req['id']}/approve", json={"yaml": "not: a-preset"})
    assert r.status_code == 400
    assert "name" in r.json()["error"]


def test_deny_roundtrip(client):
    req = _pending(client)
    res = client.post(f"/api/requests/{req['id']}/deny", json={"note": "not needed"}).json()
    assert res["request"]["state"] == "denied"


def test_auth_enforced_when_token_set(token_client):
    assert token_client.get("/api/overview").status_code == 401
    assert token_client.get("/api/overview", headers={"x-nemodeck-token": "wrong"}).status_code == 401
    assert token_client.get("/api/overview", headers={"x-nemodeck-token": "test-token-123"}).status_code == 200
    assert token_client.get("/api/overview", params={"token": "test-token-123"}).status_code == 200
    assert token_client.get("/api/overview", headers={"authorization": "Bearer test-token-123"}).status_code == 200


def test_ops_stream_sse(client):
    with client.stream("POST", "/api/sandboxes/spark-hermes/ops/doctor") as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    assert any("doctor" in (e.get("line") or "") or "ok" in (e.get("line") or "") for e in events)
    assert events[-1]["done"] is True


def test_unknown_op_rejected(client):
    r = client.post("/api/sandboxes/spark-hermes/ops/rm-rf")
    assert r.status_code == 404


def test_logs_stream_contains_blocked_line(client):
    with client.stream("GET", "/api/sandboxes/spark-hermes/ops/logs", params={"tail": 200}) as r:
        body = "".join(r.iter_text())
    assert "BLOCKED" in body