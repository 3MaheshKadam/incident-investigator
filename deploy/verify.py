"""Check a running deployment, then break it and repair it, the way an on-call would see it.

    docker compose up -d --build --wait
    python -m deploy.verify

Each step prints PASS or FAIL. Exit code 1 if any step fails. It changes the running stack
(bad release, rollback, killed service) and leaves it healthy.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

SERVICES = ["orders-db", "orders-service", "inventory-service", "api-gateway"]
Step = tuple[str, bool, str]


def compose(*args: str, env: dict[str, str] | None = None) -> str:
    p = subprocess.run(["docker", "compose", *args], capture_output=True, text=True, check=False,
                       env={**os.environ, **(env or {})})
    if p.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {p.stderr.strip()[-300:]}")
    return p.stdout + p.stderr


def status(base: str, path: str = "/checkout") -> int:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


def traffic(base: str, n: int = 40) -> float:
    """Share of requests that failed."""
    bad = sum(status(base) != 200 for _ in range(n))
    return bad / n


def wait_for(cond, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(1)
    return False


def container_health() -> dict[str, str]:
    out = compose("ps", "--format", "json")
    rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return {r["Service"]: r.get("Health") or r["State"] for r in rows}


def run(base: str) -> list[Step]:
    steps: list[Step] = []

    def record(name: str, ok: bool, detail: str) -> None:
        steps.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)

    health = container_health()
    missing = [s for s in SERVICES if health.get(s) != "healthy"]
    record("all services healthy", not missing, str(health) if missing else "4/4 healthy")
    if missing:
        return steps

    record("users can check out", traffic(base) == 0, "0% errors on 40 requests")

    compose("up", "-d", "--no-deps", "orders-service", env={"ORDERS_BUG": "1"})
    wait_for(lambda: container_health().get("orders-service") == "healthy", 30)
    rate = traffic(base, 60)
    record("bad release is visible to users", rate > 0.05, f"{rate:.0%} errors after deploying the buggy release")
    logs = compose("logs", "--no-log-prefix", "orders-service")
    record("the bug is findable in the logs", "AttributeError" in logs, "AttributeError in orders-service logs")

    compose("up", "-d", "--no-deps", "orders-service", env={"ORDERS_BUG": "0"})
    wait_for(lambda: container_health().get("orders-service") == "healthy", 30)
    rate = traffic(base)
    record("rollback restores service", rate == 0, f"{rate:.0%} errors after rollback")

    compose("kill", "-s", "KILL", "orders-service")
    down = wait_for(lambda: status(base) != 200, 10)
    record("an outage is visible to users", down, "gateway returns errors while orders-service is dead")
    back = wait_for(lambda: status(base) == 200, 60)
    record("restart policy brings it back", back, "service recovered without human action" if back
           else "no recovery within 60s")
    if not back:
        compose("up", "-d", "--no-deps", "orders-service")
        wait_for(lambda: status(base) == 200, 30)

    final = traffic(base)
    record("stack left healthy", final == 0, f"{final:.0%} errors at the end")
    return steps


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--base", default="http://127.0.0.1:8080", help="gateway URL")
    p.add_argument("--wait", type=int, default=0, help="seconds to wait for all services to be healthy")
    args = p.parse_args(argv)
    if args.wait:
        wait_for(lambda: all(container_health().get(s) == "healthy" for s in SERVICES), args.wait)
    steps = run(args.base)
    failed = [s for s in steps if not s[1]]
    print(f"\n{len(steps) - len(failed)}/{len(steps)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
