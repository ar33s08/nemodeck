"""FastAPI application: REST + SSE over the Deck service.

Auth model: a single operator token (``Authorization: Bearer`` /
``X-Nemodeck-Token`` / ``?token=``). The CLI refuses to bind a non-loopback
address without a token, so the console is never wide-open on the network.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from . import __version__
from .adapters.base import AdapterError
from .service import Deck, DeckError

WEB_DIR = Path(__file__).parent / "web"


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def create_app(deck: Deck, token: str | None = None) -> FastAPI:
    app = FastAPI(title="nemodeck", version=__version__, docs_url=None, redoc_url=None)

    index_html: str | None = None

    def require_auth(request: Request) -> None:
        if not token:
            return
        supplied = ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        supplied = supplied or request.headers.get("x-nemodeck-token") or request.query_params.get("token") or ""
        if supplied != token:
            raise HTTPException(status_code=401, detail="invalid or missing operator token")

    @app.exception_handler(AdapterError)
    async def _adapter_error(_request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=502, content={"error": str(exc)})

    @app.exception_handler(DeckError)
    async def _deck_error(_request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content={"error": str(exc)})

    # -- static -------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index():
        nonlocal index_html
        if index_html is None:
            index_html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return index_html

    @app.get("/manifest.webmanifest")
    def manifest():
        return Response((WEB_DIR / "manifest.webmanifest").read_text(encoding="utf-8"), media_type="application/manifest+json")

    @app.get("/icon.svg")
    def icon():
        return Response((WEB_DIR / "icon.svg").read_text(encoding="utf-8"), media_type="image/svg+xml")

    # -- api ------------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "adapter": deck.adapter.kind}

    @app.get("/api/overview", dependencies=[Depends(require_auth)])
    def overview():
        return deck.overview()

    @app.get("/api/sandboxes", dependencies=[Depends(require_auth)])
    def sandboxes():
        return {"sandboxes": [s.__dict__ for s in deck.sandboxes()]}

    @app.get("/api/sandboxes/{name}/status", dependencies=[Depends(require_auth)])
    def status(name: str):
        return deck.sandbox_status(name).__dict__

    @app.get("/api/sandboxes/{name}/policy", dependencies=[Depends(require_auth)])
    def policy(name: str):
        info = deck.policy(name)
        return {"sandbox": info.sandbox, "presets": info.presets, "custom_groups": info.custom_groups, "raw": info.raw}

    @app.get("/api/sandboxes/{name}/snapshots", dependencies=[Depends(require_auth)])
    def snapshots(name: str):
        res = deck.adapter.snapshot_list(name)
        return {"ok": res.ok, "output": (res.out or res.err).strip()}

    @app.get("/api/requests", dependencies=[Depends(require_auth)])
    def requests(refresh: bool = True):
        reqs = deck.refresh_requests() if refresh else deck.requests()
        return {"requests": [r.to_dict() for r in reqs], "unparsed": deck.last_unparsed[-50:]}

    @app.get("/api/requests/{rid}/snippet", dependencies=[Depends(require_auth)])
    def snippet(rid: str, method: str | None = None, path: str | None = None):
        return {"yaml": deck.snippet(rid, method=method, path=path)}

    @app.post("/api/requests/{rid}/approve", dependencies=[Depends(require_auth)])
    def approve(rid: str, payload: dict = Body(default={})):
        return deck.approve(rid, note=str(payload.get("note", "")), yaml_text=payload.get("yaml"))

    @app.post("/api/requests/{rid}/deny", dependencies=[Depends(require_auth)])
    def deny(rid: str, payload: dict = Body(default={})):
        return deck.deny(rid, note=str(payload.get("note", "")))

    @app.post("/api/sandboxes/{name}/ask", dependencies=[Depends(require_auth)])
    def ask(name: str, payload: dict = Body(default={})):
        return deck.ask(name, str(payload.get("prompt", "")))

    @app.get("/api/audit", dependencies=[Depends(require_auth)])
    def audit(tail: int = 200):
        return {"entries": deck.audit_tail(tail)}

    # -- streams ----------------------------------------------------------------
    def _op_stream(name: str, op: str, **kwargs):
        def gen():
            try:
                for line in deck.stream_op(name, op, **kwargs):
                    yield _sse({"line": line})
            except DeckError as exc:
                yield _sse({"line": f"[nemodeck] {exc}"})
            yield _sse({"done": True})

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/sandboxes/{name}/ops/{op}", dependencies=[Depends(require_auth)])
    @app.post("/api/sandboxes/{name}/ops/{op}", dependencies=[Depends(require_auth)])
    def ops_stream(name: str, op: str, follow: bool = False, tail: int = 120, snapshot_name: str | None = None):
        allowed = {"start", "stop", "rebuild", "recover", "doctor", "logs", "snapshot_create", "config_export"}
        if op not in allowed:
            raise HTTPException(status_code=404, detail=f"unknown op {op!r}; expected one of {sorted(allowed)}")
        kwargs: dict = {}
        if op == "logs":
            kwargs.update(follow=follow, tail=tail)
        if op == "snapshot_create" and snapshot_name:
            kwargs["snapshot_name"] = snapshot_name
        return _op_stream(name, op, **kwargs)

    return app