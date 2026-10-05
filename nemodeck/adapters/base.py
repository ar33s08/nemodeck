"""Adapter interface: the contract every NemoClaw backend must satisfy.

Two implementations ship in v0.1:

* ``live``  — drives the real ``nemoclaw`` / ``openshell`` CLIs on this host.
* ``demo``  — a stateful simulator, for development, demos, and CI.

The service layer (``nemodeck.service``) and HTTP API only ever see this
interface, so an OpenShell-gateway-SDK adapter can slot in later without
touching anything above it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import EgressRequest, PolicyInfo, Sandbox, SandboxStatus
from ..runner import CommandResult


class AdapterError(RuntimeError):
    """Raised when a backend cannot satisfy a request."""


class NemoClawAdapter(ABC):
    kind: str = "base"

    # -- capability ---------------------------------------------------------
    @abstractmethod
    def probe(self) -> tuple[bool, str]:
        """Return (available, human message). Never raises."""

    # -- inventory ----------------------------------------------------------
    @abstractmethod
    def list_sandboxes(self) -> list[Sandbox]: ...

    @abstractmethod
    def status(self, name: str) -> SandboxStatus: ...

    @abstractmethod
    def logs(self, name: str, tail: int = 200) -> str: ...

    # -- policy -------------------------------------------------------------
    @abstractmethod
    def policy(self, name: str) -> PolicyInfo: ...

    @abstractmethod
    def add_policy_file(self, name: str, yaml_text: str, label: str) -> CommandResult:
        """Merge a custom policy document into the sandbox's live policy."""

    @abstractmethod
    def add_policy_preset(self, name: str, preset: str) -> CommandResult: ...

    # -- approvals ----------------------------------------------------------
    @abstractmethod
    def discover_requests(self, sandboxes: list[str]) -> tuple[list[EgressRequest], list[str]]:
        """Scan for blocked egress requests. Returns (requests, unparsed)."""

    # -- lifecycle ----------------------------------------------------------
    @abstractmethod
    def op(self, name: str, op: str, **kwargs) -> CommandResult: ...

    @abstractmethod
    def stream_op(self, name: str, op: str, **kwargs):
        """Yield output lines for a long-running op (generator)."""

    @abstractmethod
    def snapshot_list(self, name: str) -> CommandResult: ...