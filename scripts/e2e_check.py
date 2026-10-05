#!/usr/bin/env python3
"""Headless browser E2E for the nemodeck console (Chrome DevTools Protocol).

Spawns a scratch headless Chrome, walks the full approval flow against a
running console, asserts the UI actually changed, and saves a screenshot.

    .venv/bin/python scripts/e2e_check.py [base_url]
"""

from __future__ import annotations

import base64
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from websockets.sync.client import connect

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
PORT = 9333

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    shutil.which("google-chrome"),
    shutil.which("chromium"),
]


def find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if c and pathlib.Path(c).exists():
            return c
    raise SystemExit("no Chrome/Chromium found")


def http_json(path: str, method: str = "GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self._id = 0

    def cmd(self, method: str, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=30))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def eval(self, expr: str):
        r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        result = r.get("result", {})
        if result.get("subtype") == "error":
            raise RuntimeError(result)
        return result.get("value")

    def wait_for(self, expr: str, timeout: float = 12.0, poll: float = 0.25):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.eval(expr)
            if last:
                return last
            time.sleep(poll)
        raise TimeoutError(f"condition never became true: {expr} (last={last!r})")


def main() -> int:
    chrome = find_chrome()
    profile = tempfile.mkdtemp(prefix="nemodeck-e2e-")
    proc = subprocess.Popen(
        [
            chrome, "--headless=new", f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--hide-scrollbars", "--window-size=1440,1000", "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = ""):
        results.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")

    try:
        for _ in range(60):
            try:
                http_json("/json/version")
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise SystemExit("chrome devtools never came up")

        target = http_json("/json/new?" + urllib.parse.quote(BASE + "/", safe=""), method="PUT")
        with connect(target["webSocketDebuggerUrl"], max_size=10 * 1024 * 1024) as ws:
            cdp = CDP(ws)
            cdp.cmd("Runtime.enable")
            cdp.cmd("Page.enable")
            cdp.wait_for("document.readyState === 'complete' && typeof refreshAll === 'function'")

            check("title", "nemodeck" in (cdp.eval("document.title") or ""), cdp.eval("document.title"))
            pending = cdp.wait_for("document.querySelectorAll('.req.pending').length || false")
            check("pending cards render", pending == 3, f"got {pending}")
            check("pending tile", cdp.eval("document.querySelector('#stPend').textContent") == "3")
            ep = cdp.eval("document.querySelector('.req.pending .ep').textContent.trim()")
            check("endpoint shown", ":" in (ep or ""), ep)

            # approve flow
            cdp.eval("document.querySelector('.req.pending .btn.acc').click()")
            cdp.wait_for("!!document.querySelector('#overlay.open')")
            yaml_head = cdp.eval("document.querySelector('#apYaml').value.slice(0, 60)")
            check("preset yaml prefilled", (yaml_head or "").startswith("preset:"), yaml_head)
            cdp.eval("document.querySelector('#apNote').value = 'e2e: verified'")
            cdp.eval("document.querySelector('#apGo').click()")
            cdp.wait_for("document.querySelectorAll('.req.approved').length === 1", timeout=15)
            check("card moved to approved", True)
            cdp.wait_for("document.querySelectorAll('.req.pending').length === 2", timeout=15)
            check("pending count decremented", cdp.eval("document.querySelector('#stPend').textContent") == "2")

            audit_txt = cdp.eval("document.querySelector('#audList').textContent")
            check("audit shows decision", "approve_request" in (audit_txt or ""), )

            shot = cdp.cmd("Page.captureScreenshot", format="png")
            out = ROOT / "artifacts" / "ui.png"
            out.parent.mkdir(exist_ok=True)
            out.write_bytes(base64.b64decode(shot["data"]))
            check("screenshot saved", out.exists(), str(out))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())