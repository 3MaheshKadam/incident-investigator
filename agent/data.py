"""Loads the evidence files of one simulated incident.

This deliberately never opens `answer.json`, so nothing the agent can reach contains the answer.
"""

from __future__ import annotations

import json
from pathlib import Path


class ScenarioData:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.id = self.path.name
        self.alert = (self.path / "alert.txt").read_text().strip()
        self.logs: list[dict] = [
            json.loads(line) for line in (self.path / "logs.jsonl").read_text().splitlines()
        ]
        self.metrics: dict[str, dict[str, list]] = json.loads((self.path / "metrics.json").read_text())
        self.deploys: list[dict] = json.loads((self.path / "deploys.json").read_text())
        self.config_changes: list[dict] = json.loads(
            (self.path / "config_changes.json").read_text()
        )
