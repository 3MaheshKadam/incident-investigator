"""GitHub Actions workflow rules (GH...)."""

from __future__ import annotations

import re
from typing import Any

from readiness.findings import Finding

SHA = re.compile(r"^[0-9a-f]{40}$")
UNTRUSTED = re.compile(
    r"\$\{\{\s*github\.(event\.(issue|pull_request|comment|review|head_commit|commits)[^}]*"
    r"(title|body|message|name|email|label|ref)|head_ref)\s*\}\}"
)


def check_workflow(data: dict, path: str) -> list[Finding]:
    out: list[Finding] = []
    # YAML 1.1 reads the key `on` as the boolean True.
    triggers: Any = data.get("on", data.get(True)) or {}
    names = triggers if isinstance(triggers, dict) else (
        [triggers] if isinstance(triggers, str) else list(triggers)
    )
    jobs = data.get("jobs") or {}

    if "permissions" not in data and not all("permissions" in (j or {}) for j in jobs.values()):
        out.append(Finding("GH001", "medium", path,
                           "No `permissions:` block, so the token gets broad default access.",
                           "Add `permissions: contents: read` at the top and widen per job."))
    if "pull_request_target" in names:
        out.append(Finding("GH003", "medium", path,
                           "Uses pull_request_target, which runs with secrets on untrusted PR code.",
                           "Use pull_request unless you need write access; never check out the PR head."))
    for job_name, job in jobs.items():
        job = job or {}
        where = f"{path} (job {job_name})"
        if "timeout-minutes" not in job:
            out.append(Finding("GH006", "low", where,
                               "No timeout-minutes; a hung job runs for 6 hours.",
                               "Set timeout-minutes on the job."))
        for step in [s for s in job.get("steps") or [] if isinstance(s, dict)]:
            uses = step.get("uses")
            if uses and not str(uses).startswith(("./", "docker://")):
                ref = str(uses).partition("@")[2]
                if not ref or ref in ("main", "master", "latest", "HEAD"):
                    out.append(Finding("GH002", "high", where, f"Action '{uses}' is not pinned.",
                                       "Pin to a release tag, ideally a full commit SHA."))
                elif not SHA.match(ref):
                    out.append(Finding("GH002", "low", where,
                                       f"Action '{uses}' is pinned to a movable tag.",
                                       "Pin to the full commit SHA to resist supply-chain tampering."))
            script = str(step.get("run") or "")
            if re.search(r"(echo|printf|cat)[^\n]*\$\{\{\s*secrets\.", script):
                out.append(Finding("GH004", "high", where, "A secret is written to the log.",
                                   "Pass it through env: and never print it."))
            if UNTRUSTED.search(script):
                out.append(Finding("GH005", "high", where,
                                   "Untrusted event text is interpolated into a shell command "
                                   "(script injection).",
                                   "Pass it as an env var and quote it: \"$TITLE\"."))
    return out
