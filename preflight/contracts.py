"""Interfaces pinned across milestones so M2, M3, and M4 can be built in parallel.

M2 produces spans and metrics; M3 reads them back and produces a DiffReport;
M4 renders that report. Without a fixed contract those three have to be built in
sequence. With one, they don't -- so this module is the seam, and changing it is
a cross-milestone decision, not a local edit.

Nothing here should import from differ/report/runner: it is the bottom of the
dependency graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# --- Metric names emitted by M2, queryable by M3 ---------------------------
# All are emitted on the dimensions eval.run_id / eval.case_id / vcs.commit_sha.
METRIC_CASE_COST_USD = "preflight.case.cost_usd"
METRIC_CASE_TOKENS = "preflight.case.tokens"
METRIC_CASE_DURATION_MS = "preflight.case.duration_ms"
METRIC_CASE_TOOL_CALLS = "preflight.case.tool_calls"
METRIC_CASE_RETRIEVAL_HOPS = "preflight.case.retrieval_hops"
METRIC_CASE_SUCCESS = "preflight.case.success"

# --- The metrics the gate compares ----------------------------------------
# `key` is what appears in preflight.yaml thresholds and in the PR comment.
GATED_METRICS = (
    "cost_usd_per_task",
    "total_tokens_per_task",
    "p95_latency_ms",
    "tool_calls_per_task",
    "retrieval_hops_per_task",
    "success_rate",
)

# Metrics where a *higher* value is worse. success_rate is the exception:
# a drop is the regression.
HIGHER_IS_WORSE = {
    "cost_usd_per_task",
    "total_tokens_per_task",
    "p95_latency_ms",
    "tool_calls_per_task",
    "retrieval_hops_per_task",
}


@dataclass
class CaseSummary:
    """Per-case aggregates for one run, as read back from SigNoz."""

    case_id: str
    spans: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    tool_calls: int = 0
    retrieval_hops: int = 0
    success: bool = True
    trace_id: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class RunSummary:
    """One whole suite run, as read back from SigNoz. Produced by query.py."""

    run_id: str
    commit_sha: str
    cases: list[CaseSummary] = field(default_factory=list)

    @property
    def case_count(self) -> int:
        return len(self.cases)

    def metric(self, key: str) -> float:
        """Compute a gated metric from the per-case rows. Single source of truth."""
        if not self.cases:
            return 0.0
        n = len(self.cases)
        if key == "cost_usd_per_task":
            return sum(c.cost_usd for c in self.cases) / n
        if key == "total_tokens_per_task":
            return sum(c.total_tokens for c in self.cases) / n
        if key == "p95_latency_ms":
            ordered = sorted(c.duration_ms for c in self.cases)
            # Nearest-rank p95; with <20 cases this is the max, which is the
            # honest answer for a suite this size.
            idx = max(0, min(n - 1, int(round(0.95 * n)) - 1))
            return ordered[idx]
        if key == "tool_calls_per_task":
            return sum(c.tool_calls for c in self.cases) / n
        if key == "retrieval_hops_per_task":
            return sum(c.retrieval_hops for c in self.cases) / n
        if key == "success_rate":
            return sum(1 for c in self.cases if c.success) / n
        raise KeyError(f"unknown metric {key!r}; expected one of {GATED_METRICS}")


@dataclass
class MetricDelta:
    """One row of the gate table."""

    key: str
    baseline: float
    candidate: float
    threshold: float
    breached: bool
    unit: Literal["usd", "tokens", "ms", "count", "ratio"] = "count"

    @property
    def delta(self) -> float:
        return self.candidate - self.baseline

    @property
    def pct_change(self) -> float:
        if self.baseline == 0:
            return 0.0 if self.candidate == 0 else float("inf")
        return (self.candidate - self.baseline) / self.baseline * 100.0


@dataclass
class DiffReport:
    """What M3 produces and M4 renders. The gate's verdict."""

    baseline_sha: str
    candidate_sha: str
    baseline_run_id: str
    candidate_run_id: str
    deltas: list[MetricDelta] = field(default_factory=list)
    baseline: RunSummary | None = None
    candidate: RunSummary | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def breached(self) -> bool:
        return any(d.breached for d in self.deltas)

    @property
    def exit_code(self) -> int:
        return 1 if self.breached else 0
