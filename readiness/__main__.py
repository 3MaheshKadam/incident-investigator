"""    python -m readiness [path] [--json] [--fail-on high|medium|low|never]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from readiness.findings import at_least
from readiness.project import check_repo


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Check a repository for production-readiness problems.")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--json", action="store_true", help="Print the report as JSON")
    p.add_argument("--out", type=Path, default=None, help="Also write the JSON report here")
    p.add_argument("--fail-on", choices=["high", "medium", "low", "never"], default="high",
                   help="Exit 1 if any finding is at least this severe")
    args = p.parse_args(argv)
    report = check_repo(args.path)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report.to_dict(), indent=1) + "\n")
    if args.json:
        print(json.dumps(report.to_dict(), indent=1))
    else:
        for f in report.findings:
            print(f"{f}\n")
        c = report.counts()
        print(f"checked {len(report.files)} files: {c['high']} high, {c['medium']} medium, {c['low']} low")
    if args.fail_on == "never":
        return 0
    return 1 if any(at_least(f.severity, args.fail_on) for f in report.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
