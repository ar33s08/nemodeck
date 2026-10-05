"""Append-only audit trail (JSONL).

NemoClaw's session approvals reset when a sandbox is destroyed and recreated.
The operator record of *why* an endpoint was opened should not. Every decision
nemodeck takes — approvals, denials, policy changes, lifecycle ops, failed
logins — lands here, one JSON object per line, fsync'd before returning.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(
        self,
        action: str,
        actor: str = "operator",
        sandbox: str | None = None,
        target: str | None = None,
        note: str | None = None,
        result: str = "ok",
        **extra,
    ) -> dict:
        entry = {
            "ts": time.time(),
            "iso": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "actor": actor,
            "action": action,
            "sandbox": sandbox,
            "target": target,
            "note": note,
            "result": result,
        }
        if extra:
            entry.update(extra)
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return entry

    def tail(self, n: int = 200) -> list[dict]:
        """Newest-last list of the last ``n`` entries (bounded read)."""

        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            return []
        out: list[dict] = []
        for line in lines[-max(n * 2, n) :]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out[-n:]