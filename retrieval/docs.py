"""Turn one incident's logs, deploys and config changes into searchable documents.

Log lines are grouped by template (numbers masked), so 33 identical errors are one document with a
count, instead of 33 near-duplicates filling the top of every result list.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from agent.data import ScenarioData

SEVERITY = {"ERROR": 2, "WARN": 1, "INFO": 0}


@dataclass
class Doc:
    id: str
    kind: str  # log | deploy | config
    text: str  # what is searched and shown
    service: str = ""
    level: str = ""
    first_ts: str = ""
    last_ts: str = ""
    count: int = 1
    meta: dict = field(default_factory=dict)


def mask(msg: str) -> str:
    return re.sub(r"\d+(\.\d+)*", "N", msg)


def build_docs(data: ScenarioData) -> list[Doc]:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in data.logs:
        groups[(r["service"], r["level"], mask(r["msg"]))].append(r)
    docs: list[Doc] = []
    for n, ((service, level, _), rows) in enumerate(sorted(groups.items())):
        sample = rows[-1]["msg"]
        times = "" if len(rows) == 1 else f" x{len(rows)} between {rows[0]['ts']} and {rows[-1]['ts']}"
        docs.append(Doc(f"log-{n}", "log", f"{level} {service}{times}: {sample}", service, level,
                        rows[0]["ts"], rows[-1]["ts"], len(rows)))
    for n, d in enumerate(data.deploys):
        docs.append(Doc(f"deploy-{n}", "deploy",
                        f"deploy {d['service']} {d['version']} by {d['author']}: {d['commit_message']}",
                        d["service"], "", d["ts"], d["ts"]))
    for n, c in enumerate(data.config_changes):
        docs.append(Doc(f"config-{n}", "config",
                        f"config change {c['service']} {c['key']}: {c['old']} -> {c['new']} by {c['author']}",
                        c["service"], "", c["ts"], c["ts"]))
    return docs


_ALERT = re.compile(r"ALERT \[(\w+)\] ([\w\-]+): .*? since (\S+)")


def parse_alert(alert: str) -> tuple[str, str]:
    """(service, start time) from an alert line, or empty strings if it does not look like one."""
    m = _ALERT.match(alert)
    return (m.group(2), m.group(3)) if m else ("", "")
