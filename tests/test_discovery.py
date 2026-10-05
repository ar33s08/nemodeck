"""Blocked-egress parsing: tolerant, honest about what it cannot parse."""

from nemodeck import discovery


def test_parses_host_port_form():
    text = "[egress] BLOCKED connect host=api.weather.gov port=443 binary=/usr/local/bin/hermes method=GET path=/points"
    reqs, unparsed = discovery.parse_blocked_lines(text, "spark-hermes")
    assert not unparsed
    assert len(reqs) == 1
    r = reqs[0]
    assert (r.host, r.port, r.binary, r.method, r.path) == ("api.weather.gov", 443, "/usr/local/bin/hermes", "GET", "/points")
    assert r.sandbox == "spark-hermes"
    assert r.state == "pending"


def test_parses_url_form():
    text = "[egress] DENIED https://files.pythonhosted.org/packages/ab/cd/w.whl — policy=default binary=/opt/venv/bin/python3"
    reqs, _ = discovery.parse_blocked_lines(text, "sb1")
    assert len(reqs) == 1
    assert reqs[0].host == "files.pythonhosted.org"
    assert reqs[0].port == 443
    assert reqs[0].binary == "/opt/venv/bin/python3"
    assert reqs[0].path and reqs[0].path.startswith("/packages/")


def test_parses_bare_hostport_form():
    text = "[egress] blocked connect registry.npmjs.org:443 client=/usr/local/bin/node"
    reqs, _ = discovery.parse_blocked_lines(text, "sb2")
    assert len(reqs) == 1
    assert reqs[0].host == "registry.npmjs.org"
    assert reqs[0].port == 443
    assert reqs[0].binary == "/usr/local/bin/node"


def test_ignores_unrelated_lines_and_reports_unparseable():
    text = "\n".join(
        [
            "[hermes] dashboard listening on 0.0.0.0:18789",
            "[egress] request blocked by policy but with no destination detail",
            "[egress] BLOCKED host=vendor.example.com port=8443 binary=/usr/bin/git method=POST path=/v1/ingest",
        ]
    )
    reqs, unparsed = discovery.parse_blocked_lines(text, "sb")
    assert len(reqs) == 1
    assert reqs[0].host == "vendor.example.com"
    assert reqs[0].port == 8443
    assert len(unparsed) == 1 and "no destination detail" in unparsed[0]


def test_dedupe_within_batch_and_stable_id():
    line = "[egress] BLOCKED host=api.weather.gov port=443 binary=/usr/local/bin/hermes method=GET path=/points"
    reqs, _ = discovery.parse_blocked_lines("\n".join([line, line, line]), "spark-hermes")
    assert len(reqs) == 1
    assert reqs[0].count == 3
    # id is stable across runs and independent of line order
    again, _ = discovery.parse_blocked_lines(line, "spark-hermes")
    assert again[0].id == reqs[0].id


def test_identical_endpoint_different_binary_are_distinct():
    a = "[egress] BLOCKED host=x.dev port=443 binary=/usr/bin/curl"
    b = "[egress] BLOCKED host=x.dev port=443 binary=/usr/bin/git"
    reqs, _ = discovery.parse_blocked_lines(a + "\n" + b, "sb")
    assert len(reqs) == 2
    assert {r.binary for r in reqs} == {"/usr/bin/curl", "/usr/bin/git"}