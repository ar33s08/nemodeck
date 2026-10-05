"""Adapter registry."""

from __future__ import annotations

from .base import AdapterError, NemoClawAdapter


def get_adapter(kind: str, **kwargs) -> NemoClawAdapter:
    kind = (kind or "demo").lower()
    if kind == "demo":
        from .demo import DemoAdapter

        return DemoAdapter(**kwargs)
    if kind == "live":
        from .live import LiveAdapter

        return LiveAdapter(**kwargs)
    raise AdapterError(f"unknown adapter: {kind!r} (expected 'demo' or 'live')")


__all__ = ["AdapterError", "NemoClawAdapter", "get_adapter"]