"""Shared fixtures: a fast demo-backed Deck and API clients."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nemodeck.adapters.demo import DemoAdapter  # noqa: E402
from nemodeck.audit import AuditLog  # noqa: E402
from nemodeck.server import create_app  # noqa: E402
from nemodeck.service import Deck  # noqa: E402
from nemodeck.state import StateStore  # noqa: E402


@pytest.fixture()
def deck(tmp_path) -> Deck:
    adapter = DemoAdapter(speed=0.0)
    store = StateStore(tmp_path / "state.json")
    audit = AuditLog(tmp_path / "audit.jsonl")
    d = Deck(adapter, store, audit)
    d.probe()
    return d


@pytest.fixture()
def client(deck):
    from fastapi.testclient import TestClient

    return TestClient(create_app(deck, token=None))


@pytest.fixture()
def token_client(deck):
    from fastapi.testclient import TestClient

    return TestClient(create_app(deck, token="test-token-123"))


@pytest.fixture()
def pending_id(deck) -> str:
    deck.refresh_requests()
    reqs = [r for r in deck.requests() if r.state == "pending"]
    assert reqs, "demo adapter should surface pending requests"
    return reqs[0].id