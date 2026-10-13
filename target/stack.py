"""The supervisor: starts the services as real processes and plays the part of the platform
around them (like a container runtime): redirects logs to files, restarts a process that exceeds
its memory limit, and records deploys and config changes as events.

Faults are applied here as real operations: SIGKILL a process, restart it with new settings,
drop a database index, flip a flag file, make a process allocate memory.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from target.common import parse_prometheus
from target.db import ensure_db

SERVICES = ["orders-db", "orders-service", "inventory-service", "api-gateway"]
ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rss_of(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except OSError:
        return 0.0


class Stack:
    def __init__(self, workdir: Path, db_rows: int = 1_000_000, mem_limit_mb: float = 150.0):
        self.workdir = Path(workdir)
        self.mem_limit_mb = mem_limit_mb
        self.db_rows = db_rows
        self.ports = {n: free_port() for n in SERVICES}
        self.procs: dict[str, subprocess.Popen] = {}
        self.versions: dict[str, str] = {n: "v1.0.0" for n in SERVICES}
        self.oom_events: list[tuple[float, str]] = []
        self.deploys: list[tuple[float, dict]] = []
        self.config_changes: list[tuple[float, dict]] = []
        self._extra_env: dict[str, dict[str, str]] = {n: {} for n in SERVICES}
        self._log_dir = self.workdir / "logs"

    # ----- lifecycle ----------------------------------------------------------------------------
    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.ports[name]}"

    def start(self, db_path: Path | None = None) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        for old in self._log_dir.glob("*.log"):
            old.unlink()
        shared = ensure_db(db_path or self.workdir.parent / f"shop-{self.db_rows}.sqlite", self.db_rows)
        self.db_path = self.workdir / "shop.sqlite"  # a private copy: a fault may drop its index
        shutil.copyfile(shared, self.db_path)
        self.flags_path = self.workdir / "flags.json"
        self.flags_path.write_text("{}")
        for name in SERVICES:  # callees first
            self._spawn(name)
        for name in SERVICES:
            self._wait_healthy(name)

    def _env(self, name: str) -> dict[str, str]:
        env = {
            **os.environ,
            "PORT": str(self.ports[name]),
            "APP_VERSION": self.versions[name],
            "DB_PATH": str(self.db_path),
            "FLAGS_PATH": str(self.flags_path),
            "ORDERS_DB_URL": self.url("orders-db"),
            "ORDERS_URL": self.url("orders-service"),
            "INVENTORY_URL": self.url("inventory-service"),
            "PYTHONPATH": str(ROOT),
        }
        env.update(self._extra_env[name])
        return env

    def _spawn(self, name: str) -> None:
        logf = open(self._log_dir / f"{name}.log", "ab")  # noqa: SIM115 - owned by the child process
        self.procs[name] = subprocess.Popen(
            [sys.executable, "-m", "target.services", name],
            stdout=logf, stderr=logf, env=self._env(name), cwd=ROOT,
        )
        logf.close()

    def _wait_healthy(self, name: str, timeout: float = 8.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.url(name) + "/health", timeout=0.5).read()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"{name} did not become healthy")

    def stop(self) -> None:
        for p in self.procs.values():
            if p.poll() is None:
                p.send_signal(signal.SIGKILL)
        for p in self.procs.values():
            p.wait()

    # ----- the platform's job: watchdog -----------------------------------------------------------
    def tick(self) -> None:
        """Called every sample. Kills and restarts a process that exceeds its memory limit."""
        for name, p in list(self.procs.items()):
            if p.poll() is None and rss_of(p.pid) > self.mem_limit_mb:
                p.send_signal(signal.SIGKILL)
                p.wait()
                self.oom_events.append((time.time(), name))
                with open(self._log_dir / f"{name}.log", "a") as f:
                    f.write(json.dumps({"ts": time.time(), "service": name, "level": "ERROR",
                                        "msg": "container restarted: OOMKilled (exit code 137)"}) + "\n")
                self._spawn(name)
                self._wait_healthy(name)

    # ----- real operations a fault is made of -----------------------------------------------------
    def deploy(self, name: str, version: str, commit: str, author: str, **env: str) -> None:
        """Ship a new release: record the event, restart the process with the new settings."""
        self.versions[name] = version
        self._extra_env[name].update(env)
        self.deploys.append((time.time(), {"service": name, "version": version,
                                           "author": author, "commit_message": commit}))
        self.procs[name].send_signal(signal.SIGTERM)
        self.procs[name].wait()
        self._spawn(name)
        self._wait_healthy(name)

    def record_deploy(self, name: str, version: str, commit: str, author: str) -> None:
        """A release with no behavioural change (docs, typos). Recorded, not restarted."""
        self.deploys.append((time.time(), {"service": name, "version": version,
                                           "author": author, "commit_message": commit}))

    def kill(self, name: str) -> None:
        """Hard-kill a service and do not bring it back: a dependency outage."""
        self.procs[name].send_signal(signal.SIGKILL)
        self.procs[name].wait()

    def set_flag(self, flag: str, value: bool, service: str, author: str) -> None:
        flags = json.loads(self.flags_path.read_text())
        old = "on" if flags.get(flag) else "off"
        flags[flag] = value
        self.flags_path.write_text(json.dumps(flags))
        self.config_changes.append((time.time(), {
            "service": service, "key": flag, "old": old, "new": "on (100%)" if value else "off",
            "author": author}))

    def record_config(self, service: str, key: str, old: str, new: str, author: str) -> None:
        self.config_changes.append((time.time(), {"service": service, "key": key, "old": old,
                                                  "new": new, "author": author}))

    def ctl(self, name: str, path: str) -> dict:
        with urllib.request.urlopen(self.url(name) + path, timeout=3) as r:
            return json.loads(r.read())

    # ----- reading telemetry ----------------------------------------------------------------------
    def scrape(self, name: str) -> dict[str, float] | None:
        try:
            with urllib.request.urlopen(self.url(name) + "/metrics", timeout=0.4) as r:
                return parse_prometheus(r.read().decode())
        except OSError:
            return None

    def read_logs(self, name: str) -> list[dict]:
        out = []
        for line in (self._log_dir / f"{name}.log").read_text(errors="ignore").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a partial line from a process killed mid-write
        return out
