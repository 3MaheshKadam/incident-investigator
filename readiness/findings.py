from __future__ import annotations

from dataclasses import asdict, dataclass

SEVERITIES = ("high", "medium", "low")


@dataclass(frozen=True)
class Finding:
    rule: str  # e.g. DF002
    severity: str  # high | medium | low
    file: str
    message: str
    fix: str
    line: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"{self.severity.upper():<6} {self.rule}  {where}\n         {self.message}\n         fix: {self.fix}"


def at_least(severity: str, threshold: str) -> bool:
    return SEVERITIES.index(severity) <= SEVERITIES.index(threshold)
