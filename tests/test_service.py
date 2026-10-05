"""Service-layer flows: merging, snippets, decisions, audit."""

from __future__ import annotations

import pytest
import yaml

from nemodeck.models import EgressRequest
from nemodeck.service import DeckError, preset_name_for, slugify


def test_refresh_is_stable_and_merges(deck):
    first = deck.refresh_requests()
    second = deck.refresh_requests()
    assert [r.id for r in first] == [r.id for r in second]
    assert all(r.state == "pending" for r in second)
    assert all(r.first_seen > 0 for r in second)


def test_snippet_is_a_valid_least_privilege_preset(deck):
    deck.refresh_requests()
    req = [r for r in deck.requests() if r.host == "api.weather.gov"][0]
    text = deck.snippet(req.id)
    doc = yaml.safe_load(text)
    assert doc["preset"]["name"].startswith("nemodeck-")
    group = doc["network_policies"][doc["preset"]["name"]]
    assert group["endpoints"][0]["host"] == "api.weather.gov"
    assert group["endpoints"][0]["port"] == 443
    assert group["endpoints"][0]["enforcement"] == "enforce"
    assert group["binaries"][0]["path"] == "/usr/local/bin/hermes"
    rule = group["endpoints"][0]["rules"][0]["allow"]
    assert rule["method"] == "GET" and rule["path"] == "/points"


def test_preset_name_slugging():
    assert preset_name_for("api.weather.gov") == "nemodeck-api-weather-gov"
    assert slugify("Weird__Host!") == "weird-host"


def test_approve_flow_persists_state_policy_and_audit(deck):
    deck.refresh_requests()
    req = [r for r in deck.requests() if r.state == "pending"][0]
    out = deck.approve(req.id, note="needed for the weather tool", yaml_text=deck.snippet(req.id))
    assert out["result"]["ok"]

    updated = deck.get_request(req.id)
    assert updated.state == "approved"
    assert updated.approved_as and updated.approved_as.startswith("nemodeck-")

    # decision survives a re-scan of the same log window
    deck.refresh_requests()
    assert deck.get_request(req.id).state == "approved"

    # audit trail recorded it with the operator's reason
    entries = deck.audit_tail(20)
    approvals = [e for e in entries if e["action"] == "approve_request"]
    assert approvals and approvals[-1]["note"] == "needed for the weather tool"
    assert approvals[-1]["result"] == "ok"

    # the live policy view reflects the applied custom group
    pol = deck.policy(req.sandbox)
    assert updated.approved_as in pol.custom_groups


def test_approve_without_binary_is_refused(deck):
    rid = "feedfacecafe0001"
    deck.store.put_request(
        rid,
        EgressRequest(id=rid, sandbox="spark-hermes", host="mystery.example.com", binary="unknown").to_dict(),
    )
    deck.store.save()
    with pytest.raises(DeckError):
        deck.approve(rid)


def test_approve_with_bad_preset_name_is_refused(deck):
    deck.refresh_requests()
    req = [r for r in deck.requests() if r.state == "pending"][0]
    with pytest.raises(DeckError):
        deck.approve(req.id, yaml_text="preset:\n  name: Bad_Name\nnetwork_policies: {}\n")


def test_deny_flow(deck):
    deck.refresh_requests()
    req = [r for r in deck.requests() if r.state == "pending"][0]
    out = deck.deny(req.id, note="not needed for this task")
    assert out["result"]["ok"]
    assert deck.get_request(req.id).state == "denied"
    entries = deck.audit_tail(10)
    assert any(e["action"] == "deny_request" and e["note"] == "not needed for this task" for e in entries)


def test_overview_reports_counts(deck):
    deck.refresh_requests()
    ov = deck.overview()
    assert ov["adapter"] == "demo"
    assert ov["counts"]["sandboxes"] == 2
    assert ov["counts"]["running"] == 1
    assert ov["counts"]["pending"] >= 1
    assert ov["error"] is None