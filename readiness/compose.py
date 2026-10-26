"""docker-compose rules (DC...)."""

from __future__ import annotations

import re
from typing import Any

from readiness.findings import Finding

SECRET_NAME = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY)", re.IGNORECASE)
DATASTORES = ("postgres", "mysql", "mariadb", "mongo", "redis", "memcached", "elasticsearch")
VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-?+][^}]*)?\}")


def _env_items(env: Any) -> list[tuple[str, str]]:
    if isinstance(env, dict):
        return [(str(k), "" if v is None else str(v)) for k, v in env.items()]
    items = []
    for entry in env or []:
        k, _, v = str(entry).partition("=")
        items.append((k, v))
    return items


def _image_unpinned(image: str) -> bool:
    if "@sha256:" in image:
        return False
    name = image.rsplit("/", 1)[-1]
    return ":" not in name or name.endswith(":latest")


def _walk_strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _walk_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_strings(v)


def _build_context(build: Any) -> str | None:
    if not build:
        return None
    return build if isinstance(build, str) else str(build.get("context", "."))


def check_compose(
    data: dict,
    path: str = "docker-compose.yml",
    env_example_keys: set[str] | None = None,
    dockerfile_healthy: dict[str, bool] | None = None,
) -> list[Finding]:
    """`env_example_keys`: names listed in .env.example. `dockerfile_healthy`: build context -> has
    a HEALTHCHECK in its Dockerfile, so a service need not repeat it."""
    out: list[Finding] = []
    services = data.get("services") or {}
    if not isinstance(services, dict) or not services:
        return [Finding("DC000", "high", path, "No services defined.", "Add a services section.")]
    if "version" in data:
        out.append(Finding("DC013", "low", path, "The top-level `version` key is obsolete.",
                           "Remove it; Compose ignores it."))

    defined_volumes = set((data.get("volumes") or {}).keys())
    for name, svc in services.items():
        svc = svc or {}
        where = f"{path} (service {name})"
        image, build = svc.get("image"), svc.get("build")
        if not image and not build:
            out.append(Finding("DC001", "high", where, "Service has neither image nor build.",
                               "Set image: or build:."))
        if image and _image_unpinned(str(image)):
            out.append(Finding("DC002", "high", where, f"Image '{image}' is not pinned to a version.",
                               "Pin a tag or digest so deploys are repeatable."))
        ctx = _build_context(build)
        covered = bool(dockerfile_healthy and ctx is not None and dockerfile_healthy.get(ctx))
        if "healthcheck" not in svc and not covered:
            out.append(Finding("DC003", "medium", where,
                               "No healthcheck, so a hung process looks healthy.",
                               "Add healthcheck: with test, interval, timeout, retries."))
        if "restart" not in svc and "deploy" not in svc:
            out.append(Finding("DC004", "medium", where, "No restart policy.",
                               "Set restart: unless-stopped (or on-failure)."))
        limits = ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        if not (svc.get("mem_limit") or limits.get("memory")):
            out.append(Finding("DC005", "medium", where,
                               "No memory limit; one leak can take the host down.",
                               "Set mem_limit or deploy.resources.limits.memory."))
        if svc.get("privileged") or svc.get("network_mode") == "host" or any(
            "docker.sock" in str(v) for v in svc.get("volumes") or []
        ):
            out.append(Finding("DC006", "high", where,
                               "Privileged mode, host networking or the Docker socket is exposed.",
                               "Drop it, or grant only the specific capability needed."))
        for key, val in _env_items(svc.get("environment")):
            if SECRET_NAME.search(key) and val and not val.startswith("$"):
                out.append(Finding("DC007", "high", where,
                                   f"Environment variable {key} has a literal secret value.",
                                   "Reference ${VAR}; keep the value in an untracked .env or a secret store."))
        deps = svc.get("depends_on") or []
        for dep in (deps.keys() if isinstance(deps, dict) else deps):
            if dep not in services:
                out.append(Finding("DC008", "high", where,
                                   f"depends_on references unknown service '{dep}'.",
                                   "Fix the name or define the service."))
        if any(d in str(image or "") for d in DATASTORES):
            for port in svc.get("ports") or []:
                text = str(port)
                if ":" in text and not text.startswith("127.0.0.1:"):
                    out.append(Finding("DC009", "high", where,
                                       f"Datastore port '{text}' is published on all interfaces.",
                                       "Bind to 127.0.0.1 or drop the port and use the internal network."))
        for vol in svc.get("volumes") or []:
            src = str(vol.get("source") if isinstance(vol, dict) else vol).split(":")[0]
            if src and not src.startswith((".", "/", "~", "$")) and src not in defined_volumes:
                out.append(Finding("DC011", "high", where, f"Named volume '{src}' is not defined.",
                                   "Add it under the top-level volumes: key."))

    if env_example_keys is not None:
        used = {
            m.group(1)
            for text in _walk_strings(data)
            for m in VAR.finditer(text)
            if not m.group(2)  # variables with a default are optional
        }
        for key in sorted(used - env_example_keys):
            out.append(Finding("DC010", "medium", path,
                               f"${{{key}}} is used but missing from .env.example.",
                               f"Document {key} in .env.example (or give it a default)."))
    return out
