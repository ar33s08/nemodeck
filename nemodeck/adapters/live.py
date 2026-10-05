"""Live adapter: drives the real ``nemoclaw`` (or ``nemohermes``) CLI.

Design rules:
* argv lists only — never a shell string.
* Prefer machine-readable output (``--json``) when the CLI offers it; fall
  back to tolerant text parsing and keep the raw text on the model so the UI
  can always show ground truth.
* Mutations set ``NEMOCLAW_NON_INTERACTIVE=1`` (documented equivalent of
  ``--yes``) so nothing can hang on a prompt.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from .. import discovery
from ..models import EgressRequest, PolicyInfo, Sandbox, SandboxStatus
from ..runner import CommandResult, Runner
from .base import AdapterError, NemoClawAdapter

_MUTATE_ENV = {"NEMOCLAW_NON_INTERACTIVE": "1", "NEMOCLAW_NO_POLICY_HINT": "1"}

_AGENTS = ("hermes", "openclaw", "langchain-deepagents-code", "deepagents")


def _dig(obj, *names):
    """Recursively find the first value under any of ``names`` (case-insensitive)."""

    lowered = {n.lower() for n in names}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in lowered and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            found = _dig(v, *names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _dig(item, *names)
            if found is not None:
                return found
    return None


class LiveAdapter(NemoClawAdapter):
    kind = "live"

    def __init__(
        self,
        cli: str = "nemoclaw",
        state_dir: Path | None = None,
        path_extra: list[str] | None = None,
        runner: Runner | None = None,
    ):
        self.cli = cli
        self.state_dir = Path(state_dir or (Path.home() / ".nemodeck"))
        env_extra: dict[str, str] = {}
        extra = path_extra or ["~/.local/bin", "~/.nemoclaw/bin", "/usr/local/bin"]
        if extra:
            import os

            parts = [str(Path(p).expanduser()) for p in extra]
            env_extra["PATH"] = os.pathsep.join(parts + [os.environ.get("PATH", "")])
        self.runner = runner or Runner(timeout=120, env_extra=env_extra)

    # -- capability ---------------------------------------------------------
    def probe(self) -> tuple[bool, str]:
        res = self.runner.run([self.cli, "--version"], timeout=20)
        if res.ok:
            return True, f"{self.cli} {res.out.strip() or res.err.strip()}"
        if res.rc == 127:
            return False, f"{self.cli} not found on PATH — install NemoClaw first"
        return False, f"{self.cli} --version failed: {res.summary()}"

    # -- inventory ----------------------------------------------------------
    def list_sandboxes(self) -> list[Sandbox]:
        names: list[str] = []
        # Try JSON first, then text for both CLIs.
        for argv in ([self.cli, "list", "--json"], ["openshell", "sandbox", "list", "--json"]):
            res = self.runner.run(argv, timeout=30)
            if res.ok and res.out.strip():
                try:
                    data = json.loads(res.out)
                    names = self._names_from_json(data)
                    if names:
                        break
                except json.JSONDecodeError:
                    pass
        if not names:
            for argv in ([self.cli, "list"], ["openshell", "sandbox", "list"]):
                res = self.runner.run(argv, timeout=30)
                if res.ok and res.out.strip():
                    names = self._names_from_text(res.out)
                    if names:
                        break
        out: list[Sandbox] = []
        for name in names:
            st = self.status(name)
            out.append(
                Sandbox(
                    name=name,
                    agent=st.agent,
                    state=st.state,
                    image=str(st.raw.get("image") or "") or None,
                )
            )
        return out

    @staticmethod
    def _names_from_json(data) -> list[str]:
        names: list[str] = []
        rows = data if isinstance(data, list) else data.get("sandboxes") or data.get("data") or []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, str):
                    names.append(row)
                elif isinstance(row, dict):
                    nm = row.get("name") or row.get("sandbox") or row.get("id")
                    if isinstance(nm, str) and nm:
                        names.append(nm)
        return names

    @staticmethod
    def _names_from_text(text: str) -> list[str]:
        names: list[str] = []
        for line in text.splitlines():
            stripped = line.strip().lstrip("•-* ").strip()
            if not stripped or stripped.lower().startswith(("name", "sandbox", "usage", "no sandboxes")):
                continue
            token = re.split(r"\s{2,}|\t|\s\|\s", stripped)[0].strip().rstrip(":")
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", token):
                names.append(token)
        # de-dup, keep order
        seen: set[str] = set()
        return [n for n in names if not (n in seen or seen.add(n))]

    def status(self, name: str) -> SandboxStatus:
        res = self.runner.run([self.cli, "sandbox", "status", name, "--json"], timeout=60)
        if res.ok and res.out.strip():
            try:
                data = json.loads(res.out)
                return self._status_from_json(name, data)
            except json.JSONDecodeError:
                pass
        # Fallback: text form
        res2 = self.runner.run([self.cli, name, "status"], timeout=60)
        text = res2.out or res2.err
        return self._status_from_text(name, text, raw=res2.out.strip()[:4000])

    @staticmethod
    def _status_from_json(name: str, data: dict) -> SandboxStatus:
        if isinstance(data, dict) and data.get("found") is False:
            return SandboxStatus(name=name, found=False, state="unregistered", raw={"found": False})
        agent = _dig(data, "agent", "agentType", "runtime") or "unknown"
        state = _dig(data, "state", "status", "phase") or "unknown"
        model = _dig(data, "model", "modelId", "servedModel")
        provider = _dig(data, "provider", "providerName")
        endpoint = _dig(data, "endpoint", "baseUrl", "inferenceRoute", "route")
        gpu = _dig(data, "gpu", "gpuProof", "accelerator")
        return SandboxStatus(
            name=name,
            found=True,
            state=str(state),
            agent=str(agent),
            model=str(model) if model else None,
            provider=str(provider) if provider else None,
            endpoint=str(endpoint) if endpoint else None,
            gpu=str(gpu) if gpu else None,
            raw={"json": data if len(json.dumps(data)) < 8000 else None},
        )

    @staticmethod
    def _status_from_text(name: str, text: str, raw: str = "") -> SandboxStatus:
        def grab(label: str) -> str | None:
            m = re.search(rf"^\s*{re.escape(label)}\s*[:=]\s*(.+?)\s*$", text, re.MULTILINE | re.IGNORECASE)
            return m.group(1).strip() if m else None

        agent = (grab("Agent") or "unknown").lower()
        if not any(a in agent for a in _AGENTS):
            agent = "unknown"
        gpu = grab("GPU")
        return SandboxStatus(
            name=name,
            found="not found" not in text.lower(),
            state=(grab("State") or grab("Status") or "unknown").lower(),
            agent=agent,
            model=grab("Model"),
            provider=grab("Provider"),
            endpoint=grab("Endpoint") or grab("Inference"),
            gpu=gpu,
            raw={"text": raw},
        )

    def logs(self, name: str, tail: int = 200) -> str:
        res = self.runner.run([self.cli, name, "logs", "--tail", str(int(tail))], timeout=90)
        return res.out if res.out.strip() else res.err

    # -- policy -------------------------------------------------------------
    def policy(self, name: str) -> PolicyInfo:
        res = self.runner.run([self.cli, name, "policy", "list"], timeout=60)
        text = res.out or res.err
        presets: list[str] = []
        custom: list[str] = []
        for line in text.splitlines():
            stripped = line.strip().lstrip("•-* ").strip()
            m = re.match(r"^([a-z0-9][a-z0-9_-]*)\b(.*)$", stripped)
            if not m:
                continue
            pname = m.group(1)
            rest = m.group(2).lower()
            if pname in ("usage", "error", "available", "applied", "policy"):
                continue
            (custom if "nemodeck-" in pname else presets if "applied" in rest or "active" in rest else presets).append(pname)
        return PolicyInfo(sandbox=name, presets=presets, custom_groups=custom, raw=text)

    def policy_get(self, name: str) -> CommandResult:
        return self.runner.run([self.cli, name, "policy", "get"], timeout=60)

    def add_policy_file(self, name: str, yaml_text: str, label: str, dry_run: bool = False) -> CommandResult:
        presets_dir = self.state_dir / "presets"
        presets_dir.mkdir(parents=True, exist_ok=True)
        path = presets_dir / f"{label}.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        argv = [self.cli, name, "policy", "add", "--from-file", str(path)]
        argv.append("--dry-run" if dry_run else "--yes")
        return self.runner.run(argv, timeout=120, env_extra=_MUTATE_ENV)

    def add_policy_preset(self, name: str, preset: str, dry_run: bool = False) -> CommandResult:
        argv = [self.cli, name, "policy", "add", preset]
        argv.append("--dry-run" if dry_run else "--yes")
        return self.runner.run(argv, timeout=120, env_extra=_MUTATE_ENV)

    # -- approvals ----------------------------------------------------------
    def discover_requests(self, sandboxes: list[str]) -> tuple[list[EgressRequest], list[str]]:
        reqs: list[EgressRequest] = []
        unparsed: list[str] = []
        for name in sandboxes:
            text = self.logs(name, tail=400)
            parsed, miss = discovery.parse_blocked_lines(text, name)
            reqs.extend(parsed)
            unparsed.extend(f"[{name}] {line}" for line in miss)
        return reqs, unparsed

    def exec_in(self, name: str, cmd: list[str]) -> CommandResult:
        return self.runner.run([self.cli, name, "exec", "--", *cmd], timeout=120)

    # -- lifecycle ----------------------------------------------------------
    def op(self, name: str, op: str, **kwargs) -> CommandResult:
        if op == "snapshot_create":
            snap = kwargs.get("snapshot_name") or "nemodeck-snapshot"
            return self.runner.run([self.cli, name, "snapshot", "create", "--name", snap], timeout=300)
        if op == "config_export":
            return self.runner.run([self.cli, "config", "export", name], timeout=60)
        if op in ("start", "stop", "rebuild", "recover", "doctor"):
            if op in ("rebuild", "recover", "start", "stop"):
                return self.runner.run([self.cli, name, op], timeout=900, env_extra=_MUTATE_ENV)
            return self.runner.run([self.cli, name, op], timeout=300)
        raise AdapterError(f"unsupported op: {op}")

    def stream_op(self, name: str, op: str, **kwargs):
        if op == "logs":
            argv = [self.cli, name, "logs", "--tail", str(int(kwargs.get("tail", 120)))]
            if kwargs.get("follow"):
                argv.append("--follow")
            yield from self.runner.stream(argv, timeout=float(kwargs.get("timeout", 120)))
            return
        if op == "rebuild":
            yield from self.runner.stream([self.cli, name, "rebuild"], timeout=1200, env_extra=_MUTATE_ENV)
            return
        if op == "recover":
            yield from self.runner.stream([self.cli, name, "recover"], timeout=600, env_extra=_MUTATE_ENV)
            return
        res = self.op(name, op, **kwargs)
        if res.out:
            yield from res.out.splitlines()
        if res.err:
            yield from res.err.splitlines()
        yield f"[nemoclaw] exit {res.rc}"

    def snapshot_list(self, name: str) -> CommandResult:
        return self.runner.run([self.cli, name, "snapshot", "list"], timeout=60)