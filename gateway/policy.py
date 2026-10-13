"""Decide whether a command may run. Pure functions: no process is started here.

Three outcomes:
  allow   a read-only command that matches the allowlist exactly
  approve a change to the system (restart, rollback); it waits for a human
  deny    anything else

The default is deny. A command is never passed to a shell, so pipes, redirects and `$(...)` have
no meaning; they are rejected up front so the model learns not to rely on them.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

ALLOW, APPROVE, DENY = "allow", "approve", "deny"

SHELL_CHARS = set(";&|<>$`\\(){}*?!\n\r")
MAX_COMMAND_CHARS = 300
SERVICE = re.compile(r"^[a-z][a-z0-9\-]{1,40}$")
_FLAGS = re.compile(r"^-[A-Za-z]{1,6}$")
_NUMBER = re.compile(r"^\d{1,4}$")

# Programs with fixed, harmless flag sets. Anything not listed is denied.
SIMPLE_FLAGS = {
    "ps": {"-e", "-f", "-ef", "-aux", "aux", "-A"},
    "free": {"-m", "-h", "-k"},
    "df": {"-h", "-k"},
    "uptime": set(),
}


@dataclass
class Decision:
    verdict: str
    reason: str = ""
    argv: list[str] = field(default_factory=list)  # what to execute (allow) or the verb (approve)
    verb: str = ""
    target: str = ""


def _deny(reason: str) -> Decision:
    return Decision(DENY, reason)


def _log_path(arg: str, log_dir: Path) -> Path | None:
    """A log file inside log_dir, after resolving `..` and symlinks. Otherwise None."""
    p = Path(arg)
    p = p if p.is_absolute() else log_dir / p
    real = Path(os.path.realpath(p))
    if real.parent != Path(os.path.realpath(log_dir)) or real.suffix != ".log":
        return None
    return real if real.is_file() else None


def check(command: str, log_dir: Path, ports: dict[str, int], services: list[str]) -> Decision:
    if not command.strip():
        return _deny("empty command")
    if len(command) > MAX_COMMAND_CHARS:
        return _deny("command too long")
    if SHELL_CHARS & set(command):
        return _deny("shell syntax is not supported (no pipes, redirects, globs or substitution)")
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return _deny(f"cannot parse command: {e}")
    prog, args = argv[0], argv[1:]

    if prog in SIMPLE_FLAGS:
        bad = [a for a in args if a not in SIMPLE_FLAGS[prog]]
        if bad:
            return _deny(f"{prog}: argument not allowed: {bad[0]!r}")
        return Decision(ALLOW, argv=argv)

    if prog == "ls":
        if args not in ([], ["logs"], ["-l"], ["-l", "logs"]):
            return _deny("ls: only the logs directory can be listed")
        return Decision(ALLOW, argv=["ls", *args[:-1], str(log_dir)] if args and args[-1] == "logs"
                        else ["ls", *args, str(log_dir)])

    if prog in ("tail", "cat", "grep"):
        return _file_command(prog, args, log_dir)

    if prog == "curl":
        return _curl(args, ports)

    if prog == "svc":
        if len(args) != 2 or args[0] not in ("restart", "rollback"):
            return _deny("svc: usage is `svc restart <service>` or `svc rollback <service>`")
        if args[1] not in services or not SERVICE.match(args[1]):
            return _deny(f"svc: unknown service {args[1]!r}; known: {', '.join(services)}")
        return Decision(APPROVE, "changes the running system", verb=args[0], target=args[1])

    return _deny(f"{prog!r} is not an allowed command")


def _file_command(prog: str, args: list[str], log_dir: Path) -> Decision:
    flags, rest = [], list(args)
    while rest and rest[0].startswith("-"):
        flags.append(rest.pop(0))
    if prog == "tail":
        # tail -n 50 file | tail file
        if flags not in ([], ["-n"]):
            return _deny("tail: only -n <count> is allowed")
        count = "30"
        if flags == ["-n"]:
            if not rest or not _NUMBER.match(rest[0]):
                return _deny("tail: -n needs a number up to 9999")
            count = rest.pop(0)
        if len(rest) != 1:
            return _deny("tail: give exactly one log file")
        path = _log_path(rest[0], log_dir)
        if path is None:
            return _deny(f"tail: {rest[0]!r} is not a log file in the logs directory")
        return Decision(ALLOW, argv=["tail", "-n", count, str(path)])
    if prog == "cat":
        if flags or len(rest) != 1:
            return _deny("cat: give exactly one log file, no flags")
        path = _log_path(rest[0], log_dir)
        if path is None:
            return _deny(f"cat: {rest[0]!r} is not a log file in the logs directory")
        return Decision(ALLOW, argv=["tail", "-n", "200", str(path)])  # capped, like a pager
    # grep [-i] [-c] [-n] pattern file
    if any(f not in ("-i", "-c", "-n") for f in flags):
        return _deny("grep: only -i, -c and -n are allowed")
    if len(rest) != 2:
        return _deny("grep: usage is `grep [-i] [-c] [-n] <text> <logfile>`")
    pattern, name = rest
    if pattern.startswith("-") or len(pattern) > 80:
        return _deny("grep: pattern must be plain text under 80 characters")
    path = _log_path(name, log_dir)
    if path is None:
        return _deny(f"grep: {name!r} is not a log file in the logs directory")
    return Decision(ALLOW, argv=["grep", "-F", *flags, "-e", pattern, str(path)])


def _curl(args: list[str], ports: dict[str, int]) -> Decision:
    allowed_flags = {"-s", "-S", "-sS", "-i", "-I"}
    flags = [a for a in args[:-1] if a in allowed_flags]
    if not args or len(flags) != len(args) - 1:
        return _deny("curl: only -s, -S, -i, -I and one URL are allowed")
    m = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):(\d{2,5})(/health|/metrics)", args[-1])
    if not m:
        return _deny("curl: only the /health and /metrics endpoints on the local stack are allowed")
    if int(m.group(1)) not in ports.values():
        return _deny("curl: that port is not one of the stack's services")
    return Decision(ALLOW, argv=["curl", "--max-time", "3", "--noproxy", "*", *flags, args[-1]])
