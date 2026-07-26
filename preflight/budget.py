"""Hard spend cap for the reference agent's LLM calls.

The whole project runs on a fixed $1 of API credit, and three agents may be
iterating against it at once. A soft "try to be careful" rule is not enough --
this ledger is the enforcement point: every real API call records its cost, and
`check()` raises before a call that would cross the cap.

Fail closed. If the ledger is unreadable, refuse to spend rather than assume $0.
"""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path

LEDGER_PATH = Path(
    os.getenv("PREFLIGHT_LEDGER", Path(__file__).resolve().parent.parent / ".preflight-spend.json")
)
DEFAULT_CAP_USD = 1.00


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Spend:
    total_usd: float
    calls: int
    cap_usd: float

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.total_usd)


def _cap() -> float:
    return float(os.getenv("PREFLIGHT_BUDGET_USD", DEFAULT_CAP_USD))


def _read(fh) -> dict:
    fh.seek(0)
    raw = fh.read().strip()
    if not raw:
        return {"total_usd": 0.0, "calls": 0}
    return json.loads(raw)


def read() -> Spend:
    """Current spend. Missing ledger means nothing has been spent yet."""
    if not LEDGER_PATH.exists():
        return Spend(0.0, 0, _cap())
    with LEDGER_PATH.open("r") as fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        try:
            data = _read(fh)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return Spend(float(data["total_usd"]), int(data["calls"]), _cap())


def check(estimated_usd: float = 0.0) -> Spend:
    """Raise if spending `estimated_usd` more would cross the cap."""
    spend = read()
    if spend.total_usd + estimated_usd > spend.cap_usd:
        raise BudgetExceeded(
            f"refusing to spend: ${spend.total_usd:.4f} already spent across "
            f"{spend.calls} calls, cap is ${spend.cap_usd:.2f}. "
            "Use PREFLIGHT_REPLAY=1 to run against recorded responses, or raise "
            "PREFLIGHT_BUDGET_USD if the human has approved more spend."
        )
    return spend


def record(cost_usd: float) -> Spend:
    """Atomically add `cost_usd` to the ledger. Call after every real API call."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            data = _read(fh)
            data["total_usd"] = float(data.get("total_usd", 0.0)) + cost_usd
            data["calls"] = int(data.get("calls", 0)) + 1
            fh.seek(0)
            fh.truncate()
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return Spend(data["total_usd"], data["calls"], _cap())
