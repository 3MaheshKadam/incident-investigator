"""Safe remediation: a fixed action set, run only against a simulation, behind human approval."""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.data import ScenarioData
from agent.store import Store
from agent.tools import ACTION_TYPES

LOW_RISK = {"open_ticket"}  # may be auto-approved; everything else waits for a human


class ActionError(ValueError):
    pass


@dataclass
class SimulatedSystem:
    """A tiny stand-in for production. Actions change this and nothing else."""

    versions: dict[str, str] = field(default_factory=dict)
    previous: dict[str, str] = field(default_factory=dict)
    restarts: dict[str, int] = field(default_factory=dict)
    flags: dict[str, str] = field(default_factory=dict)
    tickets: list[str] = field(default_factory=list)

    @classmethod
    def from_data(cls, data: ScenarioData) -> SimulatedSystem:
        s = cls()
        for d in sorted(data.deploys, key=lambda d: d["ts"]):
            if d["service"] in s.versions:
                s.previous[d["service"]] = s.versions[d["service"]]
            s.versions[d["service"]] = d["version"]
        for c in sorted(data.config_changes, key=lambda c: c["ts"]):
            s.flags[c["key"]] = c["new"]
        return s

    def apply(self, action_type: str, target: str) -> str:
        if action_type not in ACTION_TYPES:
            raise ActionError(f"action not allowed: {action_type!r}")
        if action_type == "rollback_deploy":
            old = self.previous.get(target, "previous-release")
            self.versions[target] = old
            return f"rolled back {target} to {old}"
        if action_type == "restart_service":
            self.restarts[target] = self.restarts.get(target, 0) + 1
            return f"restarted {target}"
        if action_type == "toggle_feature_flag":
            cur = self.flags.get(target, "off")
            self.flags[target] = "off" if cur.startswith("on") else "on"
            return f"flag {target}: {cur} -> {self.flags[target]}"
        self.tickets.append(target)
        return f"opened ticket: {target}"


def needs_human(action_type: str) -> bool:
    return action_type not in LOW_RISK


class ApprovalGate:
    """The agent proposes. Only an explicit human decision changes the system."""

    def __init__(self, store: Store, system: SimulatedSystem):
        self.store = store
        self.system = system

    def propose(self, scenario_id: str, report: dict, run_id: int | None = None) -> int:
        action, target = report["action_type"], report.get("action_target", "")
        if action not in ACTION_TYPES:
            raise ActionError(f"action not allowed: {action!r}")
        pid = self.store.add_proposal(
            run_id, scenario_id, action, target, report.get("summary", ""),
            list(report.get("evidence", [])), "pending",
        )
        self.store.audit("proposed", {"proposal_id": pid, "action": action, "target": target})
        if not needs_human(action):
            self._execute(pid, "auto_approved", "policy")
        return pid

    def approve(self, pid: int, by: str) -> str:
        self._require_pending(pid)
        return self._execute(pid, "approved", by)

    def reject(self, pid: int, by: str, reason: str = "") -> None:
        self._require_pending(pid)
        self.store.update_proposal(pid, "rejected", by, reason or None)
        self.store.audit("rejected", {"proposal_id": pid, "by": by, "reason": reason})

    def _require_pending(self, pid: int) -> None:
        p = self.store.get_proposal(pid)
        if p is None or p["status"] != "pending":
            raise ActionError(f"proposal {pid} is not pending")

    def _execute(self, pid: int, status: str, by: str) -> str:
        p = self.store.get_proposal(pid)
        result = self.system.apply(p["action_type"], p["target"])
        self.store.update_proposal(pid, status, by, result)
        self.store.audit(status, {"proposal_id": pid, "by": by, "result": result})
        return result
