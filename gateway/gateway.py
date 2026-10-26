"""Runs checked commands, holds changes for a human, and keeps an audit trail."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gateway.policy import ALLOW, APPROVE, check

MAX_OUTPUT_CHARS = 4000
TIMEOUT_S = 5


class Operations(Protocol):
    """What a stack must offer: where its logs are, its ports, and the two permitted changes."""

    ports: dict[str, int]

    @property
    def log_dir(self) -> Path: ...
    def restart(self, name: str) -> None: ...
    def rollback(self, name: str) -> str: ...


@dataclass
class Result:
    status: str  # ran | denied | needs_approval | approved | rejected
    output: str
    proposal_id: int | None = None

    def __str__(self) -> str:
        return self.output


class CommandGateway:
    def __init__(self, stack: Operations):
        self.stack = stack
        self.audit: list[dict] = []
        self.pending: dict[int, tuple[str, str]] = {}
        self._next_id = 1

    # ----- the agent's entry point ---------------------------------------------------------------
    def run(self, command: str) -> Result:
        refresh = getattr(self.stack, "refresh_logs", None)
        if refresh:  # a stack whose logs live elsewhere (containers) copies them into log_dir first
            refresh()
        services = list(getattr(self.stack, "services", None) or self.stack.ports)
        d = check(command, self.stack.log_dir, self.stack.ports, services)
        if d.verdict == ALLOW:
            out = self._execute(d.argv)
            self._log("ran", command, out[:200])
            return Result("ran", out)
        if d.verdict == APPROVE:
            pid = self._next_id
            self._next_id += 1
            self.pending[pid] = (d.verb, d.target)
            self._log("proposed", command, f"proposal {pid}")
            return Result(
                "needs_approval",
                f"Not run. `{d.verb} {d.target}` changes the system, so it is proposal {pid} and "
                "waits for a human. Put it in your report as the proposed action.",
                pid,
            )
        self._log("denied", command, d.reason)
        return Result("denied", f"denied: {d.reason}")

    # ----- the human's entry point ---------------------------------------------------------------
    def approve(self, proposal_id: int, by: str = "human") -> Result:
        verb, target = self.pending.pop(proposal_id)
        msg = self.stack.rollback(target) if verb == "rollback" else self._restart(target)
        self._log("approved", f"svc {verb} {target}", f"by {by}: {msg}")
        return Result("approved", msg, proposal_id)

    def reject(self, proposal_id: int, by: str = "human") -> Result:
        verb, target = self.pending.pop(proposal_id)
        self._log("rejected", f"svc {verb} {target}", f"by {by}")
        return Result("rejected", f"rejected svc {verb} {target}", proposal_id)

    # ----- internals -------------------------------------------------------------------------------
    def _restart(self, name: str) -> str:
        self.stack.restart(name)
        return f"restarted {name}"

    def _execute(self, argv: list[str]) -> str:
        try:
            p = subprocess.run(  # argv was built by the policy, never a shell string
                argv, capture_output=True, text=True, timeout=TIMEOUT_S, shell=False, check=False,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return f"error: command timed out after {TIMEOUT_S}s"
        except FileNotFoundError:
            return f"error: {argv[0]} is not installed on this host"
        out = (p.stdout + p.stderr).strip() or f"(no output, exit code {p.returncode})"
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + f"\n... truncated ({len(out)} characters)"
        return out

    def _log(self, event: str, command: str, detail: str) -> None:
        self.audit.append({"ts": time.time(), "event": event, "command": command, "detail": detail})
