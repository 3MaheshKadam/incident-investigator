"""Turn monitored episodes into investigation scenarios.

Each episode is run through the detector. If it raises an incident, the telemetry is saved in the
same folder format as `scenarios/data`, with the alert text written *by the detector*. The truth
(`answer.json`) comes from the injected fault and is never readable through the agent's tools.
So the investigator, scorer and guard work on recorded data unchanged:

    python -m monitor.record
    python -m evals.run --data scenarios/recorded --split dev
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from monitor.detect import DEFAULT_CONFIG, DetectorConfig, detect_incidents
from shop.episode import Episode, build_episode
from shop.faults import KINDS
from shop.system import METRICS, ts

STEP_MIN = 5
DEV_VARIANTS = {0, 1}
VARIANTS = 4


def downsample(values: list[float], step: int = STEP_MIN) -> list[list]:
    """Average each block of `step` one-minute points, as a metrics store would roll them up."""
    return [
        [ts(i), round(sum(chunk) / len(chunk), 2)]
        for i in range(0, len(values), step)
        if (chunk := values[i : i + step])
    ]


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1) + "\n")


def record_episode(
    ep: Episode, out_dir: Path, config: DetectorConfig = DEFAULT_CONFIG
) -> dict | None:
    """Save the episode if the monitor raised an incident for it. Returns the manifest entry."""
    s = ep.system
    incidents = detect_incidents(s.metrics, s.deploys, s.config_changes, config)
    if ep.fault is None or not incidents:
        return None
    first = min(incidents, key=lambda i: i.detected_minute)
    path = out_dir / ep.id
    path.mkdir(parents=True, exist_ok=True)
    with open(path / "logs.jsonl", "w") as f:
        rows = sorted(s.logs, key=lambda x: x[0])
        f.writelines(
            json.dumps({"ts": ts(m), "service": sv, "level": lv, "msg": msg}) + "\n"
            for m, sv, lv, msg in rows
        )
    _dump(
        path / "metrics.json",
        {
            name: {m: downsample(series[m]) for m in METRICS}
            for name, series in s.metrics.items()
        },
    )
    _dump(path / "deploys.json", sorted(s.deploys, key=lambda d: d["ts"]))
    _dump(path / "config_changes.json", sorted(s.config_changes, key=lambda d: d["ts"]))
    (path / "alert.txt").write_text(first.alert + "\n")
    _dump(path / "answer.json", ep.truth)  # hidden: never read by agent tools
    return {
        "id": ep.id,
        "kind": ep.kind,
        "split": "dev" if ep.variant in DEV_VARIANTS else "heldout",
    }


def record_all(out_dir: Path) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for kind in KINDS:
        for v in range(VARIANTS):
            entry = record_episode(build_episode(kind, v), out_dir)
            if entry:
                manifest.append(entry)
    _dump(out_dir / "manifest.json", {"scenarios": manifest})
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", type=Path, default=Path("scenarios") / "recorded")
    args = p.parse_args()
    manifest = record_all(args.out)
    print(f"Recorded {len(manifest)} incidents to {args.out}")


if __name__ == "__main__":
    main()
