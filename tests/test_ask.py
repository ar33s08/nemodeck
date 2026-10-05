"""Agent-ask flow: service layer, HTTP API, demo adapter, live response parsing."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nemodeck.server import create_app
from nemodeck.service import DeckError


def test_demo_ask_returns_reply(deck):
    reply = deck.adapter.ask("spark-hermes", "hello there")
    assert isinstance(reply, str) and len(reply) > 10


def test_service_ask_audits(deck):
    out = deck.ask("spark-hermes", "ping test")
    assert out["reply"] and out["took_ms"] >= 0
    entries = deck.audit_tail(10)
    assert any(e["action"] == "agent_ask" and e["sandbox"] == "spark-hermes" for e in entries)


def test_service_ask_rejects_empty(deck):
    with pytest.raises(DeckError):
        deck.ask("spark-hermes", "   ")


def test_service_ask_rejects_oversize(deck):
    with pytest.raises(DeckError):
        deck.ask("spark-hermes", "x" * 8001)


def test_api_ask(deck):
    client = TestClient(create_app(deck, token=None))
    r = client.post("/api/sandboxes/spark-hermes/ask", json={"prompt": "say hi"})
    assert r.status_code == 200 and r.json()["reply"]


def test_api_ask_empty_400(deck):
    client = TestClient(create_app(deck, token=None))
    r = client.post("/api/sandboxes/spark-hermes/ask", json={"prompt": ""})
    assert r.status_code == 400


def test_api_ask_requires_token_when_gated(deck):
    client = TestClient(create_app(deck, token="sekret"))
    r = client.post("/api/sandboxes/spark-hermes/ask", json={"prompt": "hi"})
    assert r.status_code == 401


def test_live_extract_reply_shape():
    from nemodeck.adapters.live import _extract_reply

    assert _extract_reply({"choices": [{"message": {"content": "PONG"}}]}) == "PONG"
    with pytest.raises(Exception):
        _extract_reply({"choices": []})