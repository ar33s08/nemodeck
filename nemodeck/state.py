"""Persisted operator state for nemodeck.

Small on purpose: one JSON document holding the bookkeeping the NemoClaw CLI
cannot answer — when a blocked request was first seen, what the operator
decided, and why. The sandbox-side truth (policy, lifecycle) always lives in
NemoClaw/OpenShell; this file never caches that.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict = {"requests": {}, "meta": {}}
        self.load()

    # -- io ---------------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = loaded
            except FileNotFoundError:
                pass
            except Exception:
                # Corrupt state must never take the console down; start clean.
                pass
            self._data.setdefault("requests", {})
            self._data.setdefault("meta", {})

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, indent=1, sort_keys=True)
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # -- data -------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def get_request(self, rid: str) -> dict | None:
        with self._lock:
            return self._data["requests"].get(rid)

    def put_request(self, rid: str, record: dict) -> None:
        with self._lock:
            self._data["requests"][rid] = record

    def requests(self) -> dict:
        with self._lock:
            return dict(self._data["requests"])