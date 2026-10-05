"""Simulator behaviors."""

from __future__ import annotations


def test_probe_and_inventory(deck):
    ok, msg = deck.probe()
    assert ok and "demo" in msg
    sbs = deck.sandboxes()
    assert {s.name for s in sbs} == {"spark-hermes", "oc-lab"}
    hermes = [s for s in sbs if s.name == "spark-hermes"][0]
    assert hermes.agent == "hermes" and hermes.state == "running"


def test_status_fields(deck):
    st = deck.sandbox_status("spark-hermes")
    assert st.found and st.model == "qwen3.8-flash-next"
    assert st.endpoint and "18300" in st.endpoint
    assert st.dashboard_url and "18789" in st.dashboard_url


def test_unknown_sandbox_raises(deck):
    import pytest

    with pytest.raises(KeyError):
        deck.sandbox_status("nope")


def test_rebuild_stream_and_snapshot(deck):
    lines = list(deck.adapter.stream_op("spark-hermes", "rebuild"))
    joined = "\n".join(lines)
    assert "rebuild" in joined.lower() and "rebuilt" in joined
    assert joined.rstrip().endswith("exit 0")

    res = deck.adapter.op("spark-hermes", "snapshot", sub="create", name_arg="test-snap")
    assert res.ok and "test-snap" in res.out
    snaps = deck.adapter.snapshot_list("spark-hermes")
    assert "test-snap" in snaps.out


def test_policy_lists_presets(deck):
    pol = deck.policy("spark-hermes")
    assert "hermes-baseline" in pol.presets
    assert pol.baseline and "hermes" in pol.baseline