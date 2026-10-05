"""Plain data shapes shared by adapters, service, and API."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class Sandbox:
    """One registered NemoClaw sandbox, as seen by the CLI."""

    name: str
    agent: str = "unknown"  # hermes | openclaw | langchain-deepagents-code | unknown
    state: str = "unknown"  # running | stopped | not ready | unknown
    image: Optional[str] = None
    created: Optional[str] = None


@dataclass
class SandboxStatus:
    """Per-sandbox status: health, inference route, forwards, GPU proof."""

    name: str
    found: bool = True
    state: str = "unknown"
    agent: str = "unknown"
    model: Optional[str] = None
    provider: Optional[str] = None
    endpoint: Optional[str] = None  # inference route, e.g. inference.local -> 127.0.0.1:18300
    gpu: Optional[str] = None
    dashboard_url: Optional[str] = None
    api_url: Optional[str] = None
    forwards: list[dict] = field(default_factory=list)
    health: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class EgressRequest:
    """A blocked outbound request awaiting an operator decision.

    ``id`` is stable for (sandbox, host, port, binary): the same endpoint
    requested again folds into this entry instead of spawning duplicates.
    """

    id: str
    sandbox: str
    host: str
    port: int = 443
    binary: str = "unknown"
    method: Optional[str] = None
    path: Optional[str] = None
    first_seen: float = 0.0
    last_seen: float = 0.0
    count: int = 1
    state: str = "pending"  # pending | approved | denied
    source: str = "logs"  # logs | gateway | demo
    note: Optional[str] = None
    approved_as: Optional[str] = None
    raw_line: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PolicyInfo:
    """A sandbox's live network policy, as far as the CLI exposes it."""

    sandbox: str
    presets: list[str] = field(default_factory=list)
    custom_groups: list[str] = field(default_factory=list)
    baseline: Optional[str] = None
    raw: str = ""


def dumps(obj: Any) -> dict:
    """asdict() that tolerates plain dicts."""

    if isinstance(obj, dict):
        return obj
    return asdict(obj)