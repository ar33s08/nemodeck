"""Stateful NemoClaw simulator: development, demos, and CI.

Simulates exactly what the live host answers: two registered sandboxes with
lifecycle state, a realistic log surface — including blocked-egress lines that
flow through the *same* parser (`nemodeck.discovery`) the live adapter uses —
policy presets and custom groups, and streaming ops with believable timing.

The point: demo mode and live mode exercise one code path everywhere above the
adapter.
"""

from __future__ import annotations

import textwrap
import time

from .. import discovery
from ..models import EgressRequest, PolicyInfo, Sandbox, SandboxStatus
from ..runner import CommandResult
from .base import NemoClawAdapter

_IMAGE_HERMES = "nemoclaw/hermes-agent@sha256:9f2c41d0a7b3e5f1"
_IMAGE_OPENCLAW = "nemoclaw/openclaw-sandbox@sha256:77aa30c9e2d4b6f0"

_BLOCK_LINES = [
    "[egress] BLOCKED connect host=api.weather.gov port=443 binary=/usr/local/bin/hermes method=GET path=/points",
    "[egress] DENIED https://files.pythonhosted.org/packages/ab/cd/requests.whl — policy=default binary=/opt/venv/bin/python3",
    "[egress] blocked connect registry.npmjs.org:443 client=/usr/local/bin/node",
]

_KNOWN_PRESETS = [
    "suggested",
    "hermes-baseline",
    "npm",
    "pypi",
    "tavily",
    "messaging-telegram",
    "personal-open-internet",
]


class DemoAdapter(NemoClawAdapter):
    kind = "demo"

    def __init__(self, speed: float = 1.0):
        self.speed = max(speed, 0.0)
        self._t0 = time.time()
        self._started = {
            "spark-hermes": self._t0 - 3 * 3600 - 12 * 60,
            "oc-lab": self._t0 - 2 * 86400,
        }
        self._state = {"spark-hermes": "running", "oc-lab": "stopped"}
        self._custom_groups: dict[str, list[str]] = {"spark-hermes": [], "oc-lab": []}
        self._presets: dict[str, list[str]] = {
            "spark-hermes": ["suggested", "hermes-baseline"],
            "oc-lab": ["suggested"],
        }
        self._snapshots: dict[str, list[str]] = {
            "spark-hermes": ["baseline (1d ago)", "before-change (2h ago)"],
            "oc-lab": [],
        }

    # -- helpers ------------------------------------------------------------
    def _sleep(self, s: float) -> None:
        time.sleep(s * self.speed)

    def _require(self, name: str) -> None:
        if name not in self._state:
            raise KeyError(name)

    def _log_text(self, name: str) -> str:
        ss = 40
        head = textwrap.dedent(
            f"""\
            2026-10-05T08:58:{ss:02d}Z [gateway] sandbox {name} state={self._state.get(name, "unknown")}
            2026-10-05T08:58:41Z [hermes] dashboard listening on 0.0.0.0:18789 (forwarded to 127.0.0.1:18789)
            2026-10-05T08:58:42Z [inference] route inference.local -> http://127.0.0.1:18300/v1 model=qwen3.8-flash-next ok
            2026-10-05T08:59:01Z [hermes] session started (channel=terminal)
            2026-10-05T09:01:17Z [hermes] tool: web_fetch https://api.weather.gov/points/29.76,-95.36
            """
        )
        mid = "\n".join(_BLOCK_LINES) + "\n" if name == "spark-hermes" else ""
        tail = textwrap.dedent(
            """\
            2026-10-05T09:01:18Z [egress] operator notified — pending approval in openshell term
            2026-10-05T09:02:55Z [health] gateway=ok sandbox=ok inference=ok gpu=proof-ok
            """
        )
        return head + mid + tail

    # -- capability ---------------------------------------------------------
    def probe(self) -> tuple[bool, str]:
        return True, "demo simulator — no NemoClaw install required"

    # -- inventory ----------------------------------------------------------
    def list_sandboxes(self) -> list[Sandbox]:
        out = []
        for name, state in self._state.items():
            agent = "hermes" if name == "spark-hermes" else "openclaw"
            image = _IMAGE_HERMES if agent == "hermes" else _IMAGE_OPENCLAW
            out.append(
                Sandbox(
                    name=name,
                    agent=agent,
                    state=state,
                    image=image,
                    created=time.strftime("%Y-%m-%d %H:%M", time.localtime(self._started[name])),
                )
            )
        return out

    def status(self, name: str) -> SandboxStatus:
        self._require(name)
        state = self._state[name]
        if name == "spark-hermes":
            return SandboxStatus(
                name=name,
                found=True,
                state=state,
                agent="hermes",
                model="qwen3.8-flash-next",
                provider="vllm (existing server, host-side)",
                endpoint="inference.local → http://127.0.0.1:18300/v1",
                gpu="NVIDIA GB10 — proof: nvidia-smi ok",
                dashboard_url="http://127.0.0.1:18789/",
                api_url="http://127.0.0.1:8642/v1",
                forwards=[
                    {"service": "dashboard", "host_port": 18789, "sandbox_port": 18789},
                    {"service": "api", "host_port": 8642, "sandbox_port": 8642},
                ],
                health={"gateway": "ok", "sandbox": "ok", "inference": "ok"},
                raw={"adapter": "demo"},
            )
        return SandboxStatus(
            name=name,
            found=True,
            state=state,
            agent="openclaw",
            model="nemotron-3-super-120b-a12b",
            provider="nvidia-endpoints",
            endpoint="inference.local → integrate.api.nvidia.com:443",
            gpu="NVIDIA GB10 — proof: nvidia-smi ok",
            dashboard_url=None,
            api_url=None,
            forwards=[],
            health={"gateway": "ok", "sandbox": state},
            raw={"adapter": "demo"},
        )

    def logs(self, name: str, tail: int = 200) -> str:
        self._require(name)
        lines = self._log_text(name).splitlines()
        return "\n".join(lines[-tail:])

    # -- policy -------------------------------------------------------------
    def policy(self, name: str) -> PolicyInfo:
        self._require(name)
        presets = list(self._presets[name])
        custom = list(self._custom_groups[name])
        raw = "presets:\n" + "\n".join(f"  - {p}" for p in presets)
        if custom:
            raw += "\ncustom:\n" + "\n".join(f"  - {c}" for c in custom)
        return PolicyInfo(
            sandbox=name,
            presets=presets,
            custom_groups=custom,
            baseline="nemoclaw-blueprint/policies/hermes-sandbox.yaml",
            raw=raw,
            available=[p for p in _KNOWN_PRESETS if p not in presets],
        )

    # -- agent ----------------------------------------------------------------
    def ask(self, name: str, prompt: str, timeout: float = 300.0) -> str:
        self._require(name)
        self._sleep(0.4)
        p = prompt.strip()
        low = p.lower()
        if any(k in low for k in ("weather", "fetch ", "http", "api.")):
            return (
                f"[demo] I reached for “{p[:120]}” and the sandbox egress policy blocked it — "
                "it's showing in the approvals inbox. Approve the endpoint and I'll retry."
            )
        return (
            f"[demo] agent({name}) received: “{p[:200]}”. In live mode this reply comes from the "
            "sandbox's real agent through its OpenAI-compatible API — its tools and network policy all apply."
        )

    def add_policy_file(self, name: str, yaml_text: str, label: str) -> CommandResult:
        self._require(name)
        self._sleep(0.15)
        n_endpoints = yaml_text.count("host:")
        if label not in self._custom_groups[name]:
            self._custom_groups[name].append(label)
        return CommandResult(
            argv=["nemoclaw", name, "policy", "add", "--from-file", "<custom-preset>.yaml"],
            rc=0,
            out=f"applied custom preset '{label}' ({n_endpoints} endpoint(s)) to running policy",
            duration=0.15,
        )

    def add_policy_preset(self, name: str, preset: str) -> CommandResult:
        self._require(name)
        self._sleep(0.1)
        if preset not in _KNOWN_PRESETS:
            return CommandResult(
                argv=["nemoclaw", name, "policy", "add", preset],
                rc=1,
                err=f"unknown preset '{preset}'",
            )
        if preset not in self._presets[name]:
            self._presets[name].append(preset)
        return CommandResult(
            argv=["nemoclaw", name, "policy", "add", preset],
            rc=0,
            out=f"preset '{preset}' applied (no changes)" if preset in self._presets[name] else f"preset '{preset}' applied",
        )

    # -- approvals ----------------------------------------------------------
    def discover_requests(self, sandboxes: list[str]) -> tuple[list[EgressRequest], list[str]]:
        reqs: list[EgressRequest] = []
        unparsed: list[str] = []
        for name in sandboxes:
            if name not in self._state or self._state[name] != "running":
                continue
            parsed, miss = discovery.parse_blocked_lines(self._log_text(name), name)
            for r in parsed:
                r.source = "demo"
            reqs.extend(parsed)
            unparsed.extend(miss)
        return reqs, unparsed

    # -- lifecycle ----------------------------------------------------------
    def op(self, name: str, op: str, **kwargs) -> CommandResult:
        self._require(name)
        self._sleep(0.1)
        argv = ["nemoclaw", name, op]
        if op == "start":
            self._state[name] = "running"
            return CommandResult(argv=argv, rc=0, out=f"sandbox '{name}' started; inference route verified")
        if op == "stop":
            self._state[name] = "stopped"
            return CommandResult(argv=argv, rc=0, out=f"sandbox '{name}' stopped")
        if op == "doctor":
            return CommandResult(
                argv=argv,
                rc=0,
                out="cli ok · docker ok · gateway ok · sandbox ok · inference ok · tunnel n/a",
            )
        if op == "config" and kwargs.get("sub") == "export":
            return CommandResult(
                argv=["nemoclaw", "config", "export", name],
                rc=0,
                out=textwrap.dedent(
                    f"""\
                    apiVersion: nemoclaw/v1
                    kind: NemoClawConfig
                    metadata:
                      name: {name}
                      agent: {"hermes" if name == "spark-hermes" else "openclaw"}
                    spec:
                      inference:
                        provider: {"vllm" if name == "spark-hermes" else "nvidia-endpoints"}
                        model: {"qwen3.8-flash-next" if name == "spark-hermes" else "nemotron-3-super-120b-a12b"}
                    # credentials: redacted by design (never exported)
                    """
                ),
            )
        if op == "snapshot" and kwargs.get("sub") == "create":
            snap = kwargs.get("name_arg") or "snapshot"
            self._snapshots[name].append(f"{snap} (just now)")
            return CommandResult(argv=argv + ["snapshot", "create", "--name", snap], rc=0, out=f"snapshot '{snap}' created")
        return CommandResult(argv=argv, rc=0, out=f"{op} complete")

    def stream_op(self, name: str, op: str, **kwargs):
        self._require(name)
        if op == "logs":
            yield from self._log_text(name).splitlines()[-kwargs.get("tail", 60) :]
            yield "[nemoclaw] exit 0"
            return
        if op == "rebuild":
            steps = [
                f"[rebuild] capturing backup of '{name}' (credentials stripped)",
                "[rebuild] resolving managed image digest …",
                "[rebuild] recreating sandbox container …",
                "[rebuild] re-applying captured OpenShell policy document …",
                "[rebuild] restoring inference route (provider/​model preserved) …",
                "[rebuild] health check: gateway=ok sandbox=ok inference=ok",
                f"[rebuild] '{name}' rebuilt",
            ]
            for step in steps:
                self._sleep(0.35)
                yield step
            yield "[nemoclaw] exit 0"
            return
        if op == "recover":
            for step in ("[recover] inspecting sandbox state …", "[recover] reattaching forwards …", "[recover] recover complete"):
                self._sleep(0.3)
                yield step
            yield "[nemoclaw] exit 0"
            return
        yield from (line for line in self.op(name, op, **kwargs).out.splitlines())
        yield "[nemoclaw] exit 0"

    def snapshot_list(self, name: str) -> CommandResult:
        self._require(name)
        snaps = self._snapshots[name] or ["(none)"]
        return CommandResult(
            argv=["nemoclaw", name, "snapshot", "list"],
            rc=0,
            out="\n".join(f"- {s}" for s in snaps),
        )