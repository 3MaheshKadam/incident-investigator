"""A running docker compose deployment, seen through the same interface as the local `Stack`.

The command gateway only needs: the published ports, where log files are, a restart and a
rollback. Containers log to stdout, so `refresh_logs` copies `docker compose logs` into files.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

SERVICES = ["api-gateway", "orders-service", "inventory-service", "orders-db"]
PUBLISHED = {"api-gateway": 8080, "orders-service": 8001, "inventory-service": 8002, "orders-db": 8003}


class DockerStack:
    def __init__(self, project_dir: Path | str = ".", log_dir: Path | None = None):
        self.project_dir = Path(project_dir)
        self.services = list(SERVICES)
        self.ports = dict(PUBLISHED)
        self._log_dir = Path(log_dir) if log_dir else Path(tempfile.mkdtemp(prefix="stack-logs-"))
        self._log_dir.mkdir(parents=True, exist_ok=True)

    @property
    def log_dir(self) -> Path:
        return self._log_dir

    def _compose(self, *args: str, env: dict[str, str] | None = None) -> str:
        import os

        p = subprocess.run(["docker", "compose", *args], cwd=self.project_dir, capture_output=True,
                           text=True, check=False, env={**os.environ, **(env or {})})
        if p.returncode != 0:
            raise RuntimeError(f"docker compose {' '.join(args)}: {p.stderr.strip()[-200:]}")
        return p.stdout + p.stderr

    def refresh_logs(self) -> None:
        for name in self.services:
            try:
                text = self._compose("logs", "--no-log-prefix", "--tail", "2000", name)
            except RuntimeError:
                continue
            (self._log_dir / f"{name}.log").write_text(text)

    def restart(self, name: str) -> None:
        self._compose("restart", name)

    def rollback(self, name: str) -> str:
        """Recreate the service from the known-good release settings."""
        self._compose("up", "-d", "--no-deps", "--force-recreate", name, env={"ORDERS_BUG": "0"})
        return f"rolled back {name} to the known-good release"
