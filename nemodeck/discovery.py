"""Parse blocked-egress signals out of NemoClaw/OpenShell log output.

The gateway surfaces denied flows live in `openshell term` (interactive) and in
sandbox/gateway logs (`nemoclaw <name> logs`). This module extracts structured
requests from log text with deliberately tolerant patterns, and returns the
lines it could not parse so live mode can show them verbatim instead of
silently dropping evidence.

Design note: the simulator adapter feeds its logs through this same parser, so
the exact code path exercised in demo mode is the one that runs against a real
sandbox.
"""

from __future__ import annotations

import hashlib
import re

from .models import EgressRequest

_BLOCK_RE = re.compile(r"\b(blocked|denied|rejected|not allowed|egress policy)\b", re.IGNORECASE)
# Mixed-in runtime logs (hermes gateway etc.) can contain OS/library errors that
# look like policy denials — "Permission denied", rejected API keys — not egress.
_NOISE_RE = re.compile(r"(permission denied|invalid api key)", re.IGNORECASE)

_HOST_PORT_RES = (
    # host=api.example.com port=443
    re.compile(r"host=(?P<host>[A-Za-z0-9][A-Za-z0-9.\-]+)(?:\s+port=(?P<port>\d{1,5}))?"),
    # https://api.example.com:443/path  or  http://api.example.com/path
    re.compile(r"https?://(?P<host>[A-Za-z0-9][A-Za-z0-9.\-]+)(?::(?P<port>\d{1,5}))?(?P<urlpath>/[^\s\"')\]]*)?"),
    # bare api.example.com:443
    re.compile(r"\b(?P<host>[A-Za-z0-9][A-Za-z0-9\-]*(?:\.[A-Za-z0-9\-]+)+):(?P<port>\d{1,5})\b"),
)

_BINARY_RE = re.compile(r"binar(?:y|ies)=(?P<binary>/\S+)")
_CLIENT_RE = re.compile(r"client=(?P<binary>/\S+)")
# NemoClaw/OCSF denial lines carry the process as: /usr/bin/curl(181651) -> host:443
_PROC_BINARY_RE = re.compile(r"(?P<binary>/[^\s()\[\]]+)\(\d+\)\s*(?:->|→)")
_METHOD_PATH_RE = re.compile(
    r"\b(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b(?:\s+|\s+path=)(?P<path>/[^\s\"')]*)"
)
_METHOD_ONLY_RE = re.compile(r"\bmethod=(?P<method>[A-Z]+)\b")
_PATH_ONLY_RE = re.compile(r"\bpath=(?P<path>/[^\s\"')]*)")


def stable_id(sandbox: str, host: str, port: int, binary: str) -> str:
    """Fold repeat attempts on the same endpoint + binary into one identity."""

    key = f"{sandbox}|{host.lower()}:{port}|{binary}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _extract_host_port(line: str) -> tuple[str, int, str | None] | None:
    for rx in _HOST_PORT_RES:
        m = rx.search(line)
        if m:
            host = m.group("host")
            port = 0
            for name in ("port", "port2"):
                try:
                    port = int(m.group(name) or 0)
                except (IndexError, ValueError):
                    port = 0
                if port:
                    break
            path = None
            try:
                path = m.group("urlpath")
            except (IndexError, KeyError):
                path = None
            return host, port or 443, path
    return None


def parse_blocked_lines(text: str, sandbox: str) -> tuple[list[EgressRequest], list[str]]:
    """Return (requests, unparsed_suspicious_lines) from raw log text."""

    reqs: list[EgressRequest] = []
    unparsed: list[str] = []
    seen: dict[str, EgressRequest] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not _BLOCK_RE.search(line):
            continue
        if _NOISE_RE.search(line):
            continue
        hp = _extract_host_port(line)
        if not hp:
            unparsed.append(line)
            continue
        host, port, url_path = hp

        binary = "unknown"
        mbin = _BINARY_RE.search(line) or _CLIENT_RE.search(line) or _PROC_BINARY_RE.search(line)
        if mbin:
            binary = mbin.group("binary")

        method = None
        path = url_path
        mm = _METHOD_PATH_RE.search(line)
        if mm:
            method = mm.group("method")
            if not path:
                path = mm.group("path")
        else:
            mm = _METHOD_ONLY_RE.search(line)
            if mm:
                method = mm.group("method")
            if not path:
                mp = _PATH_ONLY_RE.search(line)
                if mp:
                    path = mp.group("path")

        rid = stable_id(sandbox, host, port, binary)
        existing = seen.get(rid)
        if existing:
            existing.count += 1
            existing.raw_line = line
            continue
        req = EgressRequest(
            id=rid,
            sandbox=sandbox,
            host=host,
            port=port,
            binary=binary,
            method=method,
            path=path,
            source="logs",
            raw_line=line,
        )
        seen[rid] = req
        reqs.append(req)

    return reqs, unparsed