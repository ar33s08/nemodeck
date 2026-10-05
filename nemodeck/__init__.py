"""nemodeck — the operator console NemoClaw doesn't ship.

NemoClaw runs agents safely inside OpenShell sandboxes with a deny-by-default
network policy: unknown egress is blocked and surfaced to the operator for
approval. Those approvals live in a TUI, are session-scoped (they reset when a
sandbox is destroyed/recreated), and leave no trail. nemodeck turns that loop
into a web console: an approvals inbox with reasons, an append-only audit log,
one-click promotion of repeat approvals into durable policy, and lifecycle
operations — all driven through the real ``nemoclaw``/``openshell`` CLIs.
"""

__version__ = "0.1.0"