"""Try the gateway by hand: start the real stack, break it, and run commands against it.

    python -m gateway                       # scripted tour
    python -m gateway "ps -ef" "free -m"    # your own commands, run against the broken stack
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from gateway.gateway import CommandGateway
from target.stack import Stack

TOUR = [
    "ps -ef", "free -m", "ls logs", "tail -n 3 api-gateway.log", "curl -s {gw}/health",
    "curl -s {orders}/health", "grep -c refused api-gateway.log", "rm -rf /", "cat /etc/passwd",
    "ps -ef | head", "curl -s http://example.com/health", "svc restart orders-service",
]


def main(argv: list[str]) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "work"
        work.mkdir()
        stack = Stack(work, db_rows=5000)
        stack.start(db_path=Path(tmp) / "shop.sqlite")
        try:
            gw = CommandGateway(stack)
            fmt = {"gw": f"http://127.0.0.1:{stack.ports['api-gateway']}",
                   "orders": f"http://127.0.0.1:{stack.ports['orders-service']}"}
            stack.kill("orders-service")  # the fault: orders-service dies
            import urllib.request
            for _ in range(3):  # give the gateway some failing traffic to log
                try:
                    urllib.request.urlopen(fmt["gw"] + "/checkout", timeout=3).read()
                except OSError:
                    pass
            for cmd in [c.format(**fmt) for c in (argv or TOUR)]:
                r = gw.run(cmd)
                print(f"\n$ {cmd}\n[{r.status}] {r.output[:600]}")
                if r.status == "needs_approval":
                    print("-> a human approves it:")
                    print("  ", gw.approve(r.proposal_id, by="demo").output)
                    print("  health after:", gw.run(f"curl -s {fmt['orders']}/health").output[:100])
            print("\naudit log:")
            for a in gw.audit:
                print(f"  {a['event']:9} {a['command']}")
        finally:
            stack.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
