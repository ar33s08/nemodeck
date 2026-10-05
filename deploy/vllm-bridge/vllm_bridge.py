#!/usr/bin/env python3
"""vllm_bridge — expose a loopback-only model server to the OpenShell docker bridge.

Why this exists
---------------
NemoClaw routes sandbox inference to ``host.openshell.internal``, which resolves
to the host's docker-bridge address (e.g. ``172.18.0.1`` on the
``openshell-docker`` network). A vLLM/Ollama server bound only to ``127.0.0.1``
is unreachable on that path — the gateway proxy answers HTTP 502 — and OpenShell
documents the fix as "bind it to an address reachable from the gateway runtime".

This bridge does exactly that, with the smallest possible surface: it listens
ONLY on the chosen bridge interface and forwards to the loopback model server.
Nothing is exposed on the LAN or public interfaces. Verify the bind with::

    ss -tlnp | grep 18300

Find your bridge gateway address with::

    docker network inspect openshell-docker --format '{{(index .IPAM.Config 0).Gateway}}'

Usage
-----
    python3 vllm_bridge.py --listen 172.18.0.1:18300 --target 127.0.0.1:18300

Persist it with cron (user-level, no sudo)::

    @reboot sleep 30 && nohup python3 ~/bin/vllm_bridge.py --listen 172.18.0.1:18300 --target 127.0.0.1:18300 >> ~/vllm-bridge.log 2>&1 &

Stdlib only. No dependencies.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time

BUF = 65536


def _bridge(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(BUF)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass


def _handle(client: socket.socket, target: tuple[str, int], verbose: bool) -> None:
    peer = None
    try:
        peer = client.getpeername()
        upstream = socket.create_connection(target, timeout=10)
    except OSError as exc:
        if verbose:
            print(f"[vllm_bridge] upstream connect failed: {exc}", flush=True)
        client.close()
        return
    if verbose:
        print(f"[vllm_bridge] {peer[0]}:{peer[1]} -> {target[0]}:{target[1]}", flush=True)
    threading.Thread(target=_bridge, args=(client, upstream), daemon=True).start()
    _bridge(upstream, client)


def _parse_addr(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected host:port, got {value!r}")
    return host, int(port)


def main() -> int:
    ap = argparse.ArgumentParser(description="Loopback model server -> OpenShell docker bridge")
    ap.add_argument("--listen", required=True, type=_parse_addr, help="bridge address to listen on, e.g. 172.18.0.1:18300")
    ap.add_argument("--target", required=True, type=_parse_addr, help="model server address, e.g. 127.0.0.1:18300")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(args.listen)
    except OSError as exc:
        print(f"[vllm_bridge] cannot bind {args.listen[0]}:{args.listen[1]}: {exc}", file=sys.stderr)
        return 1
    srv.listen(128)
    print(f"[vllm_bridge] listening {args.listen[0]}:{args.listen[1]} -> {args.target[0]}:{args.target[1]}", flush=True)

    while True:
        try:
            client, _ = srv.accept()
        except KeyboardInterrupt:
            return 0
        except OSError:
            time.sleep(0.2)
            continue
        threading.Thread(target=_handle, args=(client, args.target, args.verbose), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())