"""Live-adapter parser calibration against REAL nemoclaw output.

The fixtures below are verbatim-captured output from NemoClaw v0.0.124
(OpenShell 0.0.116) on a DGX Spark — keep them in sync with reality; when the
CLI's output shape changes, update the fixture AND the parser together.
"""

from __future__ import annotations

import json

from nemodeck.adapters.live import LiveAdapter
from nemodeck.runner import CommandResult

# -- verbatim fixtures -------------------------------------------------------

LIST_TEXT = """\
  Sandboxes:
    spark-hermes *
      agent: hermes  model: qwen3.8-flash-next  provider: vllm-local  sandbox GPU  policies: brew, huggingface, local-inference, npm, pypi
      dashboard: http://127.0.0.1:18789/

  * = default sandbox
"""

STATUS_JSON = """\
✓ Active gateway set to 'nemoclaw'
{
  "schemaVersion": 1,
  "name": "spark-hermes",
  "found": true,
  "agent": "hermes",
  "model": "qwen3.8-flash-next",
  "provider": "vllm-local",
  "phase": "Ready",
  "gatewayState": "present",
  "inferenceHealth": {
    "ok": false,
    "probed": true,
    "endpoint": "https://inference.local/v1/models",
    "detail": "Inference gateway returned HTTP 503; the route is reachable but unhealthy.",
    "subprobes": [
      {
        "ok": false,
        "probed": true,
        "providerLabel": "Local vLLM",
        "endpoint": "http://127.0.0.1:8000/v1/models",
        "detail": "host probe failed"
      }
    ]
  },
  "hostGpuDetected": true,
  "sandboxGpuEnabled": true,
  "sandboxGpuProof": {"status": "verified", "cudaVerified": true},
  "openshellVersion": "0.0.116",
  "policies": ["brew", "huggingface", "local-inference", "npm", "pypi"]
}
"""

POLICY_LIST_TEXT = """\

  Policy presets for sandbox 'spark-hermes':
    ○ brave — Brave Search API access
    ● brew [user-added] — Homebrew (Linuxbrew) package manager access (brew binary preinstalled in base image)
    ○ github — GitHub.com and GitHub API access (git)
    ● local-inference [user-added] — Local inference access (Ollama, vLLM, llama.cpp) through the OpenShell gateway
    ● npm [user-added] — npm and Yarn registry access
    ○ personal-open-internet — Broad TCP egress on destination ports 80 and 443 for trusted personal sandboxes
    ● pypi [user-added] — Python Package Index (PyPI) access
"""


class StubRunner:
    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.calls: list[list[str]] = []

    def run(self, argv, timeout=None, env_extra=None):
        self.calls.append(list(argv))
        key = " ".join(argv)
        for needle, out in self.mapping.items():
            if needle in key:
                return CommandResult(argv=list(argv), rc=0, out=out)
        return CommandResult(argv=list(argv), rc=1, err=f"no stub for: {key}")


def make_adapter(mapping: dict[str, str]) -> LiveAdapter:
    adapter = LiveAdapter.__new__(LiveAdapter)  # bypass __init__ env wiring
    adapter.cli = "nemoclaw"
    from pathlib import Path

    adapter.state_dir = Path("/tmp/nemodeck-test")
    adapter.runner = StubRunner(mapping)
    return adapter


# -- tests -------------------------------------------------------------------

def test_list_parses_real_output():
    adapter = make_adapter({"nemoclaw list": LIST_TEXT, "sandbox status": STATUS_JSON})
    sbs = adapter.list_sandboxes()
    assert [s.name for s in sbs] == ["spark-hermes"]
    assert sbs[0].agent == "hermes"
    assert sbs[0].state == "Ready"


def test_status_parses_json_behind_banner_line():
    adapter = make_adapter({"sandbox status": STATUS_JSON})
    st = adapter.status("spark-hermes")
    assert st.found and st.state == "Ready"
    assert st.model == "qwen3.8-flash-next" and st.provider == "vllm-local"
    assert st.endpoint == "https://inference.local/v1/models"
    assert st.gpu and "verified" in st.gpu
    assert st.health["ok"] is False
    assert st.health["subprobes"][0]["label"] == "Local vLLM"


def test_status_handles_unknown_sandbox():
    adapter = make_adapter({"sandbox status": '{"found": false, "name": "nope"}'})
    st = adapter.status("nope")
    assert st.found is False and st.state == "unregistered"


def test_policy_list_parses_applied_markers():
    adapter = make_adapter({"policy list": POLICY_LIST_TEXT})
    pol = adapter.policy("spark-hermes")
    assert pol.presets == ["brew", "local-inference", "npm", "pypi"]
    assert "brave" in pol.available and "personal-open-internet" in pol.available
    # user-added marker captured in raw
    assert "[user-added]" in pol.raw


def test_status_falls_back_to_text_on_bad_json():
    text_out = "Sandbox: spark-hermes\n  Model: qwen3.8-flash-next\n  Provider: vllm-local\n  State: Ready\n  GPU: yes\n"
    adapter = make_adapter({"sandbox status": "not json at all", "spark-hermes status": text_out})
    st = adapter.status("spark-hermes")
    assert st.name == "spark-hermes"
    assert st.model == "qwen3.8-flash-next"


def test_discover_requests_uses_logs_parser():
    logs = "[egress] BLOCKED connect host=api.weather.gov port=443 binary=/usr/local/bin/hermes method=GET path=/points\n"
    adapter = make_adapter({"logs": logs})
    reqs, unparsed = adapter.discover_requests(["spark-hermes"])
    assert len(reqs) == 1 and reqs[0].host == "api.weather.gov"
    assert unparsed == []