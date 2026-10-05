#!/usr/bin/env python3
"""Build a standalone, interactive preview of the nemodeck UI.

No server needed: injects a snapshot dataset into the real index.html so the
console renders (and even simulates approvals) straight from the file system.
Used for docs, sharing, and the desktop chat preview.

    python tools/make_preview.py [output_path]
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nemodeck.service import slugify  # noqa: E402

NOW = time.time()


def _req(rid, host, port, binary, method, path, first_seen, count, state="pending", note=None, approved_as=None, sandbox="spark-hermes"):
    return {
        "id": rid, "sandbox": sandbox, "host": host, "port": port, "binary": binary,
        "method": method, "path": path, "first_seen": NOW - first_seen, "last_seen": NOW - first_seen / 3,
        "count": count, "state": state, "source": "demo", "note": note,
        "approved_as": approved_as, "raw_line": None,
    }


def build_snapshot() -> dict:
    name = lambda h: "nemodeck-" + slugify(h)  # noqa: E731
    requests = [
        _req("a1b2c3d4e5f60718", "api.weather.gov", 443, "/usr/local/bin/hermes", "GET", "/points", 840, 3,
             note="weather tool needs this for the demo", approved_as=name("api.weather.gov")),
        _req("b2c3d4e5f6071829", "files.pythonhosted.org", 443, "/opt/venv/bin/python3", None, "/packages/ab/cd/httpx-0.28.1-py3-none-any.whl", 372, 5),
        _req("c3d4e5f60718293a", "registry.npmjs.org", 443, "/usr/local/bin/node", None, None, 128, 2),
        _req("d4e5f60718293a4b", "api.stripe.com", 443, "/usr/local/bin/hermes", "POST", "/v1/charges", 3600, 1,
             state="denied", note="payments host — out of scope for this sandbox"),
    ]
    audit = [
        {"ts": NOW - 5400, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - 5400)), "actor": "operator",
         "action": "op_rebuild", "sandbox": "spark-hermes", "target": None, "note": None, "result": "ok"},
        {"ts": NOW - 3900, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - 3900)), "actor": "operator",
         "action": "op_snapshot_create", "sandbox": "spark-hermes", "target": "before-change", "note": None, "result": "ok"},
        {"ts": NOW - 3600, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - 3600)), "actor": "operator",
         "action": "deny_request", "sandbox": "spark-hermes", "target": "api.stripe.com:443",
         "note": "payments host — out of scope for this sandbox", "result": "ok"},
        {"ts": NOW - 830, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - 830)), "actor": "operator",
         "action": "approve_request", "sandbox": "spark-hermes", "target": "api.weather.gov:443",
         "note": "weather tool needs this for the demo", "result": "ok", "preset": name("api.weather.gov"),
         "binary": "/usr/local/bin/hermes", "request_id": "a1b2c3d4e5f60718"},
    ]
    return {
        "overview": {
            "adapter": "demo (snapshot)", "adapter_ok": True,
            "adapter_message": "demo simulator — no NemoClaw install required",
            "sandboxes": [
                {"name": "spark-hermes", "agent": "hermes", "state": "running", "image": None, "created": "2026-10-05 06:47"},
                {"name": "oc-lab", "agent": "openclaw", "state": "stopped", "image": None, "created": "2026-10-03 09:59"},
            ],
            "counts": {"sandboxes": 2, "running": 1, "pending": 2, "approved": 1, "denied": 1},
            "unparsed": [], "error": None,
        },
        "requests": requests,
        "audit": audit,
    }


def main() -> int:
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "artifacts" / "preview-snapshot.html"
    html = (ROOT / "nemodeck" / "web" / "index.html").read_text(encoding="utf-8")
    snap = json.dumps(build_snapshot(), ensure_ascii=False)
    inject = f"<script>window.NEMODECK_SNAPSHOT = {snap};</script>\n</head>"
    if "</head>" not in html:
        raise SystemExit("index.html has no </head>")
    html = html.replace("</head>", inject, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())