"""Find the deployment files in a repository, run the rules on them, add repo-level checks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from readiness.compose import _build_context, check_compose
from readiness.dockerfile import check_dockerfile, has_healthcheck
from readiness.findings import SEVERITIES, Finding
from readiness.workflow import check_workflow

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
             "reports", "egg-info", "tests"}
SECRET_PATTERNS = [
    ("an AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "high"),
    ("a private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "high"),
    ("a Groq API key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"), "high"),
    ("an API key (sk-...)", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}\b"), "high"),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "high"),
    ("a Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "high"),
    ("a hard-coded credential", re.compile(
        r"""(?i)\b(api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*['"]([A-Za-z0-9/+_\-]{16,})['"]"""),
     "medium"),
]
PLACEHOLDER = re.compile(r"(?i)(your|example|changeme|placeholder|xxxx|<|\$\{|dummy|fake|test)")


@dataclass
class Report:
    root: str
    files: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {s: sum(1 for f in self.findings if f.severity == s) for s in SEVERITIES}

    def to_dict(self) -> dict:
        return {"root": self.root, "files_checked": self.files, "counts": self.counts(),
                "findings": [f.to_dict() for f in self.findings]}


def _walk(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and not (set(p.relative_to(root).parts[:-1]) & SKIP_DIRS):
            yield p


def _rel(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


def _env_keys(path: Path) -> set[str]:
    keys = set()
    for line in path.read_text(errors="ignore").splitlines():
        m = re.match(r"\s*#?\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m:
            keys.add(m.group(1))
    return keys


def _load_yaml(path: Path, root: Path, report: Report):
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        report.findings.append(Finding("YAML001", "high", _rel(path, root),
                                       f"Not valid YAML: {str(exc).splitlines()[0]}",
                                       "Fix the syntax; this file will fail to load."))
        return None
    return data if isinstance(data, dict) else None


def check_repo(root: str | Path) -> Report:
    root = Path(root).resolve()
    report = Report(str(root))
    files = list(_walk(root))

    dockerfiles = [p for p in files if p.name == "Dockerfile" or p.name.startswith("Dockerfile.")]
    composes = [p for p in files if re.fullmatch(r"(docker-)?compose(\..+)?\.ya?ml", p.name)]
    workflows = [p for p in files if p.parent.as_posix().endswith(".github/workflows")
                 and p.suffix in (".yml", ".yaml")]
    env_example = next((p for p in files if p.name == ".env.example"), None)
    env_keys = _env_keys(env_example) if env_example else None

    for p in dockerfiles:
        report.files.append(_rel(p, root))
        report.findings += check_dockerfile(p.read_text(), _rel(p, root))

    for p in composes:
        report.files.append(_rel(p, root))
        data = _load_yaml(p, root, report)
        if data is None:
            continue
        healthy = {}
        for svc in (data.get("services") or {}).values():
            ctx = _build_context((svc or {}).get("build"))
            if ctx is not None:
                build = svc["build"]
                name = build.get("dockerfile", "Dockerfile") if isinstance(build, dict) else "Dockerfile"
                df = p.parent / ctx / name
                healthy[ctx] = df.is_file() and has_healthcheck(df.read_text())
        found = check_compose(data, _rel(p, root), env_keys, healthy)
        report.findings += found
        if env_example is None and "${" in p.read_text():
            report.findings.append(Finding("RP003", "medium", _rel(p, root),
                                           "Compose uses ${VARS} but the repo has no .env.example.",
                                           "Add .env.example listing every variable."))

    for p in workflows:
        report.files.append(_rel(p, root))
        data = _load_yaml(p, root, report)
        if data is not None:
            report.findings += check_workflow(data, _rel(p, root))

    _repo_level(root, files, dockerfiles, report)
    order = {s: i for i, s in enumerate(SEVERITIES)}
    report.findings.sort(key=lambda f: (order[f.severity], f.rule, f.file))
    return report


def _ignored(root: Path, name: str) -> list[str]:
    p = root / name
    if not p.is_file():
        return []
    return [ln.strip() for ln in p.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def _covers(patterns: list[str], target: str) -> bool:
    return any(p.strip("/") in (target, f"{target}*", f"*{target}", f"**/{target}") or
               p.strip("/").startswith(target) for p in patterns)


def _repo_level(root: Path, files: list[Path], dockerfiles: list[Path], report: Report) -> None:
    gi = _ignored(root, ".gitignore")
    if ((root / ".git").exists() or gi) and not _covers(gi, ".env"):
        report.findings.append(Finding("RP001", "high", ".gitignore",
                                       ".env is not git-ignored, so real keys could be committed.",
                                       "Add `.env` to .gitignore."))
    if dockerfiles:
        di = _ignored(root, ".dockerignore")
        for needed in (".env", ".git"):
            if not _covers(di, needed):
                report.findings.append(Finding(
                    "RP002", "medium", ".dockerignore",
                    f"{needed} is not excluded from the Docker build context"
                    + (" (no .dockerignore file)" if not di else "") + ".",
                    f"Create/extend .dockerignore with {needed}."))
    for p in files:
        if p.stat().st_size > 1_000_000 or p.suffix in {".png", ".jpg", ".gif", ".sqlite", ".pyc"}:
            continue
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for label, rx, sev in SECRET_PATTERNS:
                m = rx.search(line)
                if m and not PLACEHOLDER.search(line):
                    report.findings.append(Finding(
                        "RP004", sev, _rel(p, root), f"Looks like {label} committed to the repository.",
                        "Remove it, rotate the key, and load it from the environment.", n))
                    break
