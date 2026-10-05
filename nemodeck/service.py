"""Deck service: the operator logic between the adapters and the HTTP API.

Responsibilities:
* merge discovered blocked requests with persisted operator decisions
  (first-seen, counts, approve/deny state) — the CLI gives us events, the
  store gives us memory;
* generate least-privilege custom-preset YAML from a request, so "approve"
  is never a bare yes: it is a reviewable policy document, applied through
  the real ``nemoclaw <name> policy add --from-file`` flow;
* write the audit trail for every decision and lifecycle action.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .adapters.base import AdapterError, NemoClawAdapter
from .audit import AuditLog
from .models import EgressRequest, PolicyInfo, Sandbox, SandboxStatus
from .state import StateStore

_RFC1123 = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-") or "endpoint"


def preset_name_for(host: str) -> str:
    return f"nemodeck-{slugify(host)}"


class DeckError(RuntimeError):
    pass


class Deck:
    def __init__(
        self,
        adapter: NemoClawAdapter,
        store: StateStore,
        audit: AuditLog,
        clock=time.time,
    ):
        self.adapter = adapter
        self.store = store
        self.audit = audit
        self.clock = clock
        self.last_unparsed: list[str] = []
        self.adapter_ok, self.adapter_msg = False, "not probed"

    # -- capability ---------------------------------------------------------
    def probe(self) -> tuple[bool, str]:
        self.adapter_ok, self.adapter_msg = self.adapter.probe()
        return self.adapter_ok, self.adapter_msg

    # -- inventory ----------------------------------------------------------
    def sandboxes(self) -> list[Sandbox]:
        try:
            return self.adapter.list_sandboxes()
        except AdapterError:
            raise
        except Exception as exc:  # keep the console alive on parser surprises
            raise AdapterError(f"listing sandboxes failed: {exc}") from exc

    def sandbox_status(self, name: str) -> SandboxStatus:
        return self.adapter.status(name)

    def policy(self, name: str) -> PolicyInfo:
        return self.adapter.policy(name)

    # -- requests -----------------------------------------------------------
    def refresh_requests(self) -> list[EgressRequest]:
        """Pull blocked requests from the adapter and merge with memory."""

        running = [s.name for s in self.sandboxes() if s.state.startswith("run")]
        try:
            found, unparsed = self.adapter.discover_requests(running)
        except Exception as exc:
            raise AdapterError(f"request discovery failed: {exc}") from exc
        self.last_unparsed = unparsed

        now = self.clock()
        for req in found:
            rec = self.store.get_request(req.id)
            if rec:
                rec["last_seen"] = now
                # counts reflect occurrences in the newest log window, not scan
                # repetitions of the same window
                rec["count"] = max(int(rec.get("count", 1)), req.count)
                rec["binary"] = req.binary if req.binary != "unknown" else rec.get("binary", "unknown")
                rec["method"] = req.method or rec.get("method")
                rec["path"] = req.path or rec.get("path")
                rec["raw_line"] = req.raw_line
                # decision state is operator memory — it survives re-discovery
                self.store.put_request(req.id, rec)
            else:
                d = req.to_dict()
                d["first_seen"] = now
                d["last_seen"] = now
                self.store.put_request(req.id, d)
        self.store.save()
        return self.requests()

    def requests(self) -> list[EgressRequest]:
        recs = self.store.requests()
        out = [EgressRequest(**{k: v for k, v in rec.items() if k in EgressRequest.__dataclass_fields__}) for rec in recs.values()]
        order = {"pending": 0, "approved": 1, "denied": 2}
        out.sort(key=lambda r: (order.get(r.state, 9), -r.last_seen))
        return out

    def get_request(self, rid: str) -> EgressRequest:
        rec = self.store.get_request(rid)
        if not rec:
            raise DeckError(f"unknown request id: {rid}")
        return EgressRequest(**{k: v for k, v in rec.items() if k in EgressRequest.__dataclass_fields__})

    # -- snippet generation ---------------------------------------------------
    def snippet(self, rid: str, method: str | None = None, path: str | None = None) -> str:
        """Render the custom-preset YAML for a blocked request.

        Schema follows NemoClaw's documented custom-preset format
        (docs/network-policy/create-custom-policy-presets).
        """

        req = self.get_request(rid)
        name = preset_name_for(req.host)
        if not _RFC1123.match(name):
            raise DeckError(f"cannot derive a valid preset name from {req.host!r}")
        method = (method or req.method or "*").upper()
        path = path or req.path or "/**"
        binary = req.binary if req.binary != "unknown" else "<PATH-FROM-openshell-term>"
        return (
            "preset:\n"
            f"  name: {name}\n"
            f"  description: \"nemodeck approval — {req.host}:{req.port} for {req.sandbox}\"\n"
            "network_policies:\n"
            f"  {name}:\n"
            f"    name: {name}\n"
            "    endpoints:\n"
            f"      - host: {req.host}\n"
            f"        port: {req.port}\n"
            "        protocol: rest\n"
            "        enforcement: enforce\n"
            "        rules:\n"
            f"          - allow: {{ method: \"{method}\", path: \"{path}\" }}\n"
            "    binaries:\n"
            f"      - {{ path: {binary} }}\n"
        )

    # -- decisions ------------------------------------------------------------
    def approve(self, rid: str, note: str = "", actor: str = "operator", yaml_text: str | None = None) -> dict:
        req = self.get_request(rid)
        if req.state == "approved":
            raise DeckError("request is already approved")
        yaml_text = yaml_text if yaml_text is not None else self.snippet(rid)
        if "<PATH-FROM-openshell-term>" in yaml_text:
            raise DeckError(
                "the requesting binary is unknown — edit the preset and set the exact "
                "binary path that openshell term reports for this request"
            )
        label = None
        m = re.search(r"^\s*name:\s*([a-z0-9-]+)\s*$", yaml_text, re.MULTILINE)
        if m:
            label = m.group(1)
        if not label or not _RFC1123.match(label):
            raise DeckError("preset YAML must contain a valid RFC 1123 'name:' label")

        res = self.adapter.add_policy_file(req.sandbox, yaml_text, label)
        target = f"{req.host}:{req.port}"
        if res.ok:
            self.audit.append(
                "approve_request", actor=actor, sandbox=req.sandbox, target=target,
                note=note, result="ok", preset=label, binary=req.binary, request_id=rid,
            )
            rec = self.store.get_request(rid) or {}
            rec["state"] = "approved"
            rec["approved_as"] = label
            rec["note"] = note or rec.get("note")
            self.store.put_request(rid, rec)
            self.store.save()
        else:
            self.audit.append(
                "approve_request", actor=actor, sandbox=req.sandbox, target=target,
                note=note, result=f"error: {res.summary()}", preset=label, request_id=rid,
            )
        return {"request": self.get_request(rid).to_dict(), "result": {"ok": res.ok, "rc": res.rc, "output": (res.out or res.err).strip()}}

    def deny(self, rid: str, note: str = "", actor: str = "operator") -> dict:
        req = self.get_request(rid)
        self.audit.append(
            "deny_request", actor=actor, sandbox=req.sandbox, target=f"{req.host}:{req.port}",
            note=note, result="ok", request_id=rid,
        )
        rec = self.store.get_request(rid) or {}
        rec["state"] = "denied"
        rec["note"] = note or rec.get("note")
        self.store.put_request(rid, rec)
        self.store.save()
        return {"request": self.get_request(rid).to_dict(), "result": {"ok": True, "rc": 0, "output": "denied — endpoint stays blocked"}}

    # -- ops / audit ----------------------------------------------------------
    def run_op(self, name: str, op: str, actor: str = "operator", snapshot_name: str | None = None) -> dict:
        kwargs = {"snapshot_name": snapshot_name} if snapshot_name else {}
        res = self.adapter.op(name, op, **kwargs)
        self.audit.append(
            f"op_{op}", actor=actor, sandbox=name, target=snapshot_name, result="ok" if res.ok else f"error: {res.summary()}",
        )
        return {"ok": res.ok, "rc": res.rc, "output": (res.out or res.err).strip()}

    def stream_op(self, name: str, op: str, actor: str = "operator", **kwargs):
        self.audit.append(f"op_{op}", actor=actor, sandbox=name, result="started", **({"follow": True} if kwargs.get("follow") else {}))
        try:
            for line in self.adapter.stream_op(name, op, **kwargs):
                yield line
            self.audit.append(f"op_{op}", actor=actor, sandbox=name, result="finished")
        except Exception as exc:  # never let the stream die silently
            self.audit.append(f"op_{op}", actor=actor, sandbox=name, result=f"error: {exc}")
            yield f"[nemodeck] error: {exc}"

    def audit_tail(self, n: int = 200) -> list[dict]:
        return self.audit.tail(n)

    # -- summary --------------------------------------------------------------
    def overview(self) -> dict:
        error = None
        sandboxes: list[dict] = []
        try:
            sandboxes = [
                {"name": s.name, "agent": s.agent, "state": s.state, "image": s.image, "created": s.created}
                for s in self.sandboxes()
            ]
        except AdapterError as exc:
            error = str(exc)
        reqs = self.requests()
        pending = [r for r in reqs if r.state == "pending"]
        return {
            "adapter": self.adapter.kind,
            "adapter_ok": self.adapter_ok,
            "adapter_message": self.adapter_msg,
            "sandboxes": sandboxes,
            "counts": {
                "sandboxes": len(sandboxes),
                "running": sum(1 for s in sandboxes if s["state"].startswith("run")),
                "pending": len(pending),
                "approved": sum(1 for r in reqs if r.state == "approved"),
                "denied": sum(1 for r in reqs if r.state == "denied"),
            },
            "unparsed": self.last_unparsed[-20:],
            "error": error,
        }