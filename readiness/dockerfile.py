"""Dockerfile rules (DF...). Looks at the final build stage, which is what actually ships."""

from __future__ import annotations

import re
import shlex

from readiness.findings import Finding

SECRET_NAME = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY)", re.IGNORECASE)


def parse(text: str) -> list[tuple[int, str, str]]:
    """Return (line number, INSTRUCTION, arguments), joining backslash continuations."""
    out, buf, start = [], "", 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if not buf:
            start = n
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        word, _, rest = buf.partition(" ")
        out.append((start, word.upper(), rest.strip()))
        buf = ""
    return out


def _pairs(args: str) -> list[tuple[str, str]]:
    """ENV/ARG accept `KEY=value KEY2=value2` or the older `KEY value`."""
    try:
        tokens = shlex.split(args)
    except ValueError:
        return []
    if tokens and "=" in tokens[0]:
        return [tuple(t.split("=", 1)) for t in tokens if "=" in t]  # type: ignore[misc]
    return [(tokens[0], " ".join(tokens[1:]))] if len(tokens) >= 2 else []


def _stages(instrs):
    stages: list[list] = []
    for item in instrs:
        if item[1] == "FROM":
            stages.append([])
        if stages:
            stages[-1].append(item)
    return stages


def has_healthcheck(text: str) -> bool:
    final = (_stages(parse(text)) or [[]])[-1]
    return any(i[1] == "HEALTHCHECK" and not i[2].upper().startswith("NONE") for i in final)


def check_dockerfile(text: str, path: str = "Dockerfile") -> list[Finding]:
    instrs = parse(text)
    stages = _stages(instrs)
    out: list[Finding] = []
    if not stages:
        return [Finding("DF000", "high", path, "No FROM instruction.", "Start from a base image.")]

    stage_names = {m.group(1).lower() for _, w, a in instrs if w == "FROM"
                   and (m := re.search(r"\bas\s+(\S+)", a, re.IGNORECASE))}
    for line, word, args in instrs:
        if word == "FROM":
            image = args.split()[0] if args else ""
            if image.lower() in stage_names or image.lower() == "scratch" or "@sha256:" in image:
                continue
            name = image.rsplit("/", 1)[-1]
            if ":" not in name or name.endswith(":latest"):
                out.append(Finding(
                    "DF001", "high", path,
                    f"Base image '{image}' is not pinned to a version, so builds change over time.",
                    "Use a specific tag (python:3.11-slim) or a digest.", line))
        if word in ("ENV", "ARG"):
            for key, val in _pairs(args):
                if SECRET_NAME.search(key) and val and not val.startswith("$"):
                    out.append(Finding(
                        "DF005", "high", path,
                        f"{word} {key} looks like a secret with a literal value baked into the image.",
                        "Pass secrets at run time (env or secret mounts), never in the image.", line))
        if word == "ADD" and not re.search(r"\.(tar|tgz|tar\.gz|tar\.xz)\b", args):
            out.append(Finding(
                "DF004", "low", path, "ADD used for a plain file or URL.",
                "Use COPY. ADD also fetches URLs and unpacks archives, which is surprising.", line))
        if word == "RUN":
            if re.search(r"(curl|wget)[^|]*\|\s*(sudo\s+)?(ba)?sh", args):
                out.append(Finding(
                    "DF009", "high", path, "Pipes a download straight into a shell.",
                    "Download, verify a checksum, then run.", line))
            if "pip install" in args and "--no-cache-dir" not in args:
                out.append(Finding(
                    "DF006", "low", path, "pip install without --no-cache-dir bloats the image.",
                    "Add --no-cache-dir.", line))
            if "apt-get install" in args and "rm -rf /var/lib/apt/lists" not in args:
                out.append(Finding(
                    "DF007", "low", path, "apt-get install without cleaning the package lists.",
                    "Add --no-install-recommends and rm -rf /var/lib/apt/lists/*.", line))

    final = stages[-1]
    users = [a for _, w, a in final if w == "USER"]
    if not users or users[-1].split(":")[0] in ("root", "0"):
        out.append(Finding(
            "DF002", "medium", path, "The container runs as root.",
            "Create an unprivileged user and add USER app before CMD."))
    if not any(w == "HEALTHCHECK" for _, w, _ in final):
        out.append(Finding(
            "DF003", "low", path, "No HEALTHCHECK in the image.",
            "Add HEALTHCHECK, or define a healthcheck in docker-compose / the orchestrator."))
    return out
