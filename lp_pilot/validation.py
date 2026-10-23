"""Validation checks and the combined run status."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    def __post_init__(self):
        if self.status not in {"PASS", "WARN", "FAIL", "BLOCKED"}:
            raise ValueError("Invalid validation status")


def final_status(checks: list[Check], critical: list[str], mode: str) -> str:
    """Combine checks, requiring all critical gates to pass for real runs."""
    indexed = {c.name: c for c in checks}
    if any(c.status == "FAIL" for c in checks):
        return "FAIL"
    if any(name not in indexed or indexed[name].status == "BLOCKED" for name in critical):
        return "BLOCKED"
    if mode != "real" or any(indexed[name].status != "PASS" for name in critical):
        return "WARN"
    return "PASS"


def serialize(checks: list[Check]) -> list[dict]:
    return [asdict(check) for check in checks]
