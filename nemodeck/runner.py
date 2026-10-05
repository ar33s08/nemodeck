"""Safe subprocess execution.

Hard rule: commands are argv lists, never shell strings. Sandbox names and
operator input are passed as discrete arguments, so a hostile name cannot
escape into a shell. Everything has a timeout; output is bounded.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field


@dataclass
class CommandResult:
    argv: list[str]
    rc: int
    out: str = ""
    err: str = ""
    duration: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out

    def summary(self) -> str:
        """One line fit for an audit note."""
        for stream in (self.err, self.out):
            for line in stream.splitlines():
                line = line.strip()
                if line:
                    return line[:300]
        return f"exit {self.rc}"


def _clamp(text: str, max_bytes: int) -> str:
    if len(text) <= max_bytes:
        return text
    head = text[: max_bytes // 2]
    tail = text[-max_bytes // 2 :]
    return f"{head}\n… [{len(text) - max_bytes} chars elided] …\n{tail}"


class Runner:
    def __init__(
        self,
        timeout: float = 90.0,
        max_bytes: int = 400_000,
        env_extra: dict[str, str] | None = None,
        cwd: str | None = None,
    ):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.env_extra = env_extra or {}
        self.cwd = cwd

    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.env_extra)
        if extra:
            env.update(extra)
        return env

    def run(
        self,
        argv: list[str],
        timeout: float | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> CommandResult:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout or self.timeout,
                env=self._env(env_extra),
                cwd=self.cwd,
            )
            return CommandResult(
                argv=list(argv),
                rc=proc.returncode,
                out=_clamp(proc.stdout or "", self.max_bytes),
                err=_clamp(proc.stderr or "", self.max_bytes),
                duration=time.monotonic() - started,
            )
        except FileNotFoundError:
            return CommandResult(
                argv=list(argv),
                rc=127,
                err=f"{argv[0]}: command not found",
                duration=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            err = exc.stderr or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
            return CommandResult(
                argv=list(argv),
                rc=-1,
                out=_clamp(out, self.max_bytes),
                err=_clamp(err + f"\n[timed out after {timeout or self.timeout}s]", self.max_bytes),
                duration=time.monotonic() - started,
                timed_out=True,
            )

    def stream(self, argv: list[str], timeout: float | None = None, env_extra: dict[str, str] | None = None):
        """Yield output lines live; kill the process at the deadline.

        Used for SSE streaming of long ops (rebuild, logs --follow).
        A watchdog thread enforces the deadline even while no output arrives.
        """

        import threading

        limit = timeout or self.timeout
        deadline = time.monotonic() + limit
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                env=self._env(env_extra),
                cwd=self.cwd,
                start_new_session=True,
            )
        except FileNotFoundError:
            yield f"{argv[0]}: command not found"
            return

        state = {"timed_out": False}

        def _watchdog():
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            if proc.poll() is None:
                state["timed_out"] = True
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass

        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()

        completed = False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                yield line.rstrip("\n")
            completed = True
        except GeneratorExit:
            raise
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        pass
            rc = proc.wait()

        if completed:
            if state["timed_out"]:
                yield f"[nemodeck] timed out after {limit}s — stopped"
            yield f"[nemodeck] exit {rc}"