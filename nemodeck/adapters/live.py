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
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import discovery
from ..models import EgressRequest, PolicyInfo, Sandbox, SandboxStatus
from ..runner import CommandResult, Runner
from .base import AdapterError, NemoClawAdapter

_MUTATE_ENV = {"NEMOCLAW_NON_INTERACTIVE": "1", "NEMOCLAW_NO_POLICY_HINT": "1"}

_HERMES_API_PORT = 8642  # host-side forward to the hermes agent's OpenAI-compatible API

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


def _load_json_with_prefix(text: str):
    """Some NemoClaw commands print a ✓ banner line before the JSON body."""

    idx = text.find("{")
    if idx < 0:
        return None
    try:
        return json.loads(text[idx:])
    except json.JSONDecodeError:
        return None


def _extract_reply(data: dict) -> str:
    """Pull the assistant text out of an OpenAI-compatible chat response."""

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AdapterError(f"unexpected chat response shape: {str(data)[:200]}") from exc
    if content is None:
        raise AdapterError("agent returned no content")
    return content


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
                data = _load_json_with_prefix(res.out)
                if data is not None:
                    names = self._names_from_json(data)
                    if names:
                        break
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
        """Parse the real `nemoclaw list` shape:

            Sandboxes:
              spark-hermes *
                agent: hermes  model: ...  provider: ...

            * = default sandbox
        """

        names: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("*") and "=" in stripped:
                continue
            stripped = stripped.lstrip("-•* ").strip()
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,63})(?:\s+\*)?$", stripped)
            if m:
                names.append(m.group(1))
        seen: set[str] = set()
        return [n for n in names if not (n in seen or seen.add(n))]

    def status(self, name: str) -> SandboxStatus:
        res = self.runner.run([self.cli, "sandbox", "status", name, "--json"], timeout=90)
        if res.ok and res.out.strip():
            data = _load_json_with_prefix(res.out)
            if data is not None:
                return self._status_from_json(name, data)
        # Fallback: text form
        res2 = self.runner.run([self.cli, name, "status"], timeout=90)
        text = res2.out or res2.err
        return self._status_from_text(name, text, raw=res2.out.strip()[:4000])

    @staticmethod
    def _status_from_json(name: str, data: dict) -> SandboxStatus:
        if isinstance(data, dict) and data.get("found") is False:
            return SandboxStatus(name=name, found=False, state="unregistered", raw={"found": False})
        health_raw = data.get("inferenceHealth") or {}
        gpu_proof = data.get("sandboxGpuProof") or {}
        state = data.get("phase") or _dig(data, "state", "status") or "unknown"
        agent = data.get("agent") or _dig(data, "agentType", "runtime") or "unknown"
        model = data.get("model") or _dig(data, "modelId", "servedModel")
        provider = data.get("provider") or _dig(data, "providerName")
        endpoint = health_raw.get("endpoint") or _dig(data, "endpoint", "baseUrl", "inferenceRoute")
        gpu = None
        if gpu_proof.get("status"):
            gpu = f"{gpu_proof.get('status')}" + (" (cuda verified)" if gpu_proof.get("cudaVerified") else "")
        elif data.get("sandboxGpuEnabled"):
            gpu = "enabled"
        health = {
            "ok": bool(health_raw.get("ok")),
            "detail": health_raw.get("detail"),
            "subprobes": [
                {"ok": p.get("ok"), "label": p.get("providerLabel"), "endpoint": p.get("endpoint"), "detail": p.get("detail")}
                for p in (health_raw.get("subprobes") or [])
            ],
        }
        return SandboxStatus(
            name=name,
            found=True,
            state=str(state),
            agent=str(agent),
            model=str(model) if model else None,
            provider=str(provider) if provider else None,
            endpoint=str(endpoint) if endpoint else None,
            gpu=gpu,
            health=health,
            raw={"json": data if len(json.dumps(data)) < 12000 else None},
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
        """Parse the real `policy list` shape:

            Policy presets for sandbox 'spark-hermes':
              ○ brave — Brave Search API access
              ● brew [user-added] — Homebrew access
        """

        res = self.runner.run([self.cli, name, "policy", "list"], timeout=60)
        text = res.out or res.err
        applied: list[str] = []
        available: list[str] = []
        custom: list[str] = []
        for line in text.splitlines():
            m = re.match(r"^\s*([●○])\s+([a-z0-9][a-z0-9_-]*)", line)
            if not m:
                continue
            pname = m.group(2)
            if m.group(1) == "●":
                applied.append(pname)
                if "nemodeck-" in pname:
                    custom.append(pname)
            else:
                available.append(pname)
        return PolicyInfo(sandbox=name, presets=applied, custom_groups=custom, baseline=None, raw=text, available=available)

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

    # -- inference ------------------------------------------------------------
    def inference_get(self) -> CommandResult:
        return self.runner.run(["openshell", "inference", "get"], timeout=30)

    def inference_set(self, provider: str, model: str, sandbox: str | None = None) -> CommandResult:
        argv = [self.cli, "inference", "set", "--provider", provider, "--model", model]
        if sandbox:
            argv += ["--sandbox", sandbox]
        return self.runner.run(argv, timeout=300, env_extra=_MUTATE_ENV)

    # -- agent ----------------------------------------------------------------
    def _gateway_token(self, name: str) -> str:
        """Cache the sandbox agent's bearer token for 10 minutes."""

        tokens = self.__dict__.setdefault("_tokens", {})
        now = time.monotonic()
        cached = tokens.get(name)
        if cached and cached[1] > now:
            return cached[0]
        res = self.runner.run([self.cli, name, "gateway-token", "--quiet"], timeout=30)
        token = (res.out or "").strip()
        if not res.ok or not token:
            raise AdapterError(f"could not read the sandbox agent token: {res.summary()}")
        tokens[name] = (token, now + 600)
        return token

    def ask(self, name: str, prompt: str, timeout: float = 300.0) -> str:
        """One agent turn through the sandbox's OpenAI-compatible endpoint.

        NemoClaw forwards the hermes agent's API to 127.0.0.1:8642 on the host
        (``openshell … forward service <sandbox> --target-port 8642``); the
        request itself runs inside the sandbox, so all its policy applies.
        """

        token = self._gateway_token(name)
        models = self.__dict__.setdefault("_agent_models", {})
        payload = json.dumps(
            {
                "model": models.get(name, "hermes-agent"),
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4096,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{_HERMES_API_PORT}/v1/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise AdapterError(f"agent endpoint HTTP {exc.code}: {body or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise AdapterError(
                f"agent endpoint unreachable at 127.0.0.1:{_HERMES_API_PORT} — "
                f"is the sandbox running and its forward healthy? ({exc.reason})"
            ) from exc
        except TimeoutError as exc:
            raise AdapterError(f"agent turn timed out after {int(timeout)}s") from exc
        return _extract_reply(data)

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