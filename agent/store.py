"""SQLite store for runs, proposals and the audit log. No server needed."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from agent.investigator import RunResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    stop_reason TEXT NOT NULL,
    steps INTEGER NOT NULL,
    report_json TEXT,
    trace_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    scenario_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    result TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ----- runs ----------------------------------------------------------------------------
    def save_run(self, r: RunResult) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (created_at, scenario_id, stop_reason, steps, report_json, trace_json)"
            " VALUES (?,?,?,?,?,?)",
            (
                _now(),
                r.scenario_id,
                r.stop_reason,
                r.steps,
                json.dumps(r.report) if r.report else None,
                json.dumps(r.trace),
            ),
        )
        self.db.commit()
        self.audit("run_saved", {"run_id": cur.lastrowid, "scenario_id": r.scenario_id})
        return int(cur.lastrowid)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["report"] = json.loads(d.pop("report_json")) if d["report_json"] else None
        d.pop("report_json", None)
        d["trace"] = json.loads(d.pop("trace_json"))
        return d

    def list_runs(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id, created_at, scenario_id, stop_reason, steps FROM runs ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ----- proposals -----------------------------------------------------------------------
    def add_proposal(self, run_id: int | None, scenario_id: str, action_type: str, target: str,
                     summary: str, evidence: list[str], status: str, result: str | None = None,
                     decided_by: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO proposals (run_id, scenario_id, action_type, target, summary,"
            " evidence_json, status, decided_by, decided_at, result) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, scenario_id, action_type, target, summary, json.dumps(evidence), status,
             decided_by, _now() if decided_by else None, result),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def update_proposal(self, pid: int, status: str, decided_by: str, result: str | None) -> None:
        self.db.execute(
            "UPDATE proposals SET status=?, decided_by=?, decided_at=?, result=? WHERE id=?",
            (status, decided_by, _now(), result, pid),
        )
        self.db.commit()

    def get_proposal(self, pid: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["evidence"] = json.loads(d.pop("evidence_json"))
        return d

    def list_proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT id FROM proposals", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        ids = [r["id"] for r in self.db.execute(q + " ORDER BY id DESC", args)]
        return [self.get_proposal(i) for i in ids]  # type: ignore[misc]

    # ----- audit ---------------------------------------------------------------------------
    def audit(self, event: str, detail: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO audit (ts, event, detail_json) VALUES (?,?,?)",
            (_now(), event, json.dumps(detail)),
        )
        self.db.commit()

    def list_audit(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM audit ORDER BY id").fetchall()
        return [
            {"ts": r["ts"], "event": r["event"], "detail": json.loads(r["detail_json"])}
            for r in rows
        ]
