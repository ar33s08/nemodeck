"""nemodeck CLI: ``python -m nemodeck [serve|selftest] ...``"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

from . import __version__
from .adapters import AdapterError, get_adapter
from .audit import AuditLog
from .server import create_app
from .service import Deck
from .state import StateStore

DEFAULT_STATE_DIR = Path.home() / ".nemodeck"


def build_deck(args) -> Deck:
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else DEFAULT_STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    if args.adapter == "live":
        import shutil

        cli = args.cli
        if not shutil.which(cli):
            for candidate in ("nemoclaw", "nemohermes"):
                if shutil.which(candidate):
                    cli = candidate
                    break
        adapter = get_adapter("live", cli=cli, state_dir=state_dir)
    else:
        adapter = get_adapter("demo")
    store = StateStore(state_dir / "state.json")
    audit = AuditLog(state_dir / "audit.jsonl")
    deck = Deck(adapter, store, audit)
    deck.probe()
    return deck


def cmd_serve(args) -> int:
    deck = build_deck(args)
    token = args.token
    host = args.host
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not token:
        token = secrets.token_urlsafe(24)

    app = create_app(deck, token=token)

    url = f"http://{host}:{args.port}/"
    banner = [
        "",
        "  nemodeck  —  operator console for NemoClaw sandboxes",
        f"  adapter : {deck.adapter.kind}  ({deck.adapter_msg})",
        f"  url     : {url}",
    ]
    if token:
        banner.append(f"  token   : {token}")
        banner.append("  share only over private networks (tailnet/VPN) — never the public internet")
    if not loopback:
        banner.append("  bind    : non-loopback — token required and enabled")
    banner.append("")
    print("\n".join(banner), flush=True)

    import uvicorn

    uvicorn.run(app, host=host, port=args.port, log_level="warning")
    return 0


def cmd_selftest(args) -> int:
    deck = build_deck(args)
    ok, msg = deck.probe()
    print(f"adapter : {deck.adapter.kind}")
    print(f"probe   : {'ok' if ok else 'FAIL'} — {msg}")
    try:
        for s in deck.sandboxes():
            print(f"sandbox : {s.name}  agent={s.agent}  state={s.state}")
    except AdapterError as exc:
        print(f"sandboxes: error — {exc}")
        return 1
    reqs = deck.refresh_requests()
    print(f"requests: {len(reqs)} known ({sum(1 for r in reqs if r.state == 'pending')} pending)")
    for r in reqs[:10]:
        print(f"  - [{r.state}] {r.host}:{r.port}  bin={r.binary}  x{r.count}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nemodeck", description="Operator console for NVIDIA NemoClaw sandboxes")
    parser.add_argument("--version", action="version", version=f"nemodeck {__version__}")
    sub = parser.add_subparsers(dest="command")

    def common(p):
        p.add_argument("--adapter", choices=["demo", "live"], default="demo", help="backend (default: demo)")
        p.add_argument("--state-dir", default=None, help="operator state directory (default: ~/.nemodeck)")
        p.add_argument("--cli", default="nemoclaw", help="NemoClaw CLI binary for the live adapter")

    p_serve = sub.add_parser("serve", help="run the console")
    common(p_serve)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.add_argument("--token", default=None, help="operator token (auto-generated for non-loopback binds)")
    p_serve.set_defaults(func=cmd_serve)

    p_self = sub.add_parser("selftest", help="probe the configured adapter without serving")
    common(p_self)
    p_self.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())