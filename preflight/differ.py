"""Baseline vs candidate: the gate.

Given two commit SHAs, resolve each to its most recent suite run *in SigNoz*,
pull both runs back, and compare them on the metrics in
`contracts.GATED_METRICS`. Every number here came out of
`/api/v5/query_range` -- there is no local results file, which is the design bet
of the whole project.

Two rules this module keeps deliberately:

1. **It does not do the metric arithmetic.** `RunSummary.metric()` in
   `contracts.py` is the single source of truth, so the differ and M4's report
   can never disagree about what "cost per task" means.
2. **It fails loudly, not quietly.** A missing baseline, a missing candidate, or
   two runs over different case sets are all reported, not papered over. A gate
   that silently passes because it found nothing to compare is worse than no
   gate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from preflight import query as query_mod
from preflight.config import Config
from preflight.contracts import (
    GATED_METRICS,
    HIGHER_IS_WORSE,
    CaseSummary,
    DiffReport,
    MetricDelta,
    RunSummary,
)
from preflight.query import Aggregation, SigNozClient, SigNozError

# How far back to look for a run on a given SHA. A CI job diffs a run it made
# minutes ago against a baseline that may be a day old, so this is generous;
# `--lookback-minutes` narrows it.
DEFAULT_LOOKBACK_MINUTES = 24 * 60

# Unit tags for the report table. Keys match GATED_METRICS exactly.
UNITS: dict[str, str] = {
    "cost_usd_per_task": "usd",
    "total_tokens_per_task": "tokens",
    "p95_latency_ms": "ms",
    "tool_calls_per_task": "count",
    "retrieval_hops_per_task": "count",
    "success_rate": "ratio",
}

# preflight.yaml key for each metric's threshold. Everything higher-is-worse is
# gated on a *percentage rise*; success_rate is gated on an *absolute drop*,
# because a percentage of a ratio is a confusing thing to put in a PR comment.
THRESHOLD_KEYS: dict[str, str] = {
    "cost_usd_per_task": "cost_usd_per_task_pct",
    "total_tokens_per_task": "total_tokens_per_task_pct",
    "p95_latency_ms": "p95_latency_ms_pct",
    "tool_calls_per_task": "tool_calls_per_task_pct",
    "retrieval_hops_per_task": "retrieval_hops_per_task_pct",
    "success_rate": "success_rate_abs_drop",
}

# Used only when preflight.yaml is missing a key -- the committed config is the
# real source. Deliberately permissive: an unconfigured metric should not be the
# thing that reddens a build.
FALLBACK_THRESHOLDS: dict[str, float] = {
    "cost_usd_per_task": 25.0,
    "total_tokens_per_task": 25.0,
    "p95_latency_ms": 75.0,
    "tool_calls_per_task": 40.0,
    "retrieval_hops_per_task": 50.0,
    "success_rate": 0.01,
}

# Absolute movement a metric must ALSO clear before a percentage breach counts.
# Percentages of small numbers are noise amplifiers: an agent whose cases take
# 0.4ms of wall time triples to 1.2ms on scheduler jitter alone and reports a
# +200% "latency regression". Observed, not theorised -- it is what the first
# end-to-end run of this gate did. Latency is the only metric with this problem
# (cost and token counts have no floor-adjacent regime), so it is the only one
# with a floor.
FLOOR_KEYS: dict[str, str] = {"p95_latency_ms": "p95_latency_ms_abs_floor_ms"}
FALLBACK_FLOORS: dict[str, float] = {"p95_latency_ms": 25.0}

_EPS = 1e-9

# Filter expressions qualify the field context explicitly. An unqualified
# `vcs.commit_sha = '...'` resolves to the *resource* attribute, which
# `otel.py` stamps once per process -- so a process that emitted runs for two
# commits would match neither correctly, and, worse, a normal CI run matches by
# coincidence. `attribute.` pins it to the span attribute `RunContext` stamps,
# which is the one the gate means. Verified against SigNoz v0.134.0.
_SHA_FIELD = "attribute.vcs.commit_sha"
_RUN_FIELD = "attribute.eval.run_id"
_ROLE_FIELD = "attribute.preflight.span_role"
_CASE_FIELD = "eval.case_id"


class DiffError(RuntimeError):
    """The diff cannot be computed -- not the same thing as a regression."""


@dataclass(frozen=True)
class RunRef:
    """A run in SigNoz, identified by SHA lookup."""

    run_id: str
    commit_sha: str
    last_seen_epoch: float
    span_count: int
    sibling_run_ids: tuple[str, ...] = ()


# --- SHA -> run id ---------------------------------------------------------


def resolve_run(
    client: SigNozClient,
    commit_sha: str,
    *,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    now_ms: int | None = None,
) -> RunRef:
    """Find the most recent `eval.run_id` recorded against a commit SHA.

    One scalar query, grouped by `eval.run_id`, aggregating `count()` and
    `max(timestamp)` -- verified working against SigNoz v0.134.0, where
    `max(timestamp)` comes back as epoch *seconds* (float).

    A SHA that was run more than once (a re-run, a retry) resolves to the newest
    run: CI's most recent word on that commit is the one the gate should use.
    """
    end_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    start_ms = end_ms - lookback_minutes * 60_000

    rows = client.scalar(
        aggregations=[
            Aggregation("count()", "spans"),
            Aggregation("max(timestamp)", "last_seen"),
        ],
        filter_expression=f"{_SHA_FIELD} = '{commit_sha}'",
        group_by=["eval.run_id"],
        start_ms=start_ms,
        end_ms=end_ms,
    )
    rows = [r for r in rows if r.get("eval.run_id")]
    if not rows:
        raise DiffError(
            f"no suite run found in SigNoz for commit {commit_sha[:12]} in the last "
            f"{lookback_minutes} minutes. Run `preflight run` on that commit "
            f"(PREFLIGHT_COMMIT_SHA={commit_sha[:12]}...) or widen --lookback-minutes."
        )

    ordered = sorted(rows, key=lambda r: float(r.get("last_seen") or 0), reverse=True)
    winner = ordered[0]
    return RunRef(
        run_id=str(winner["eval.run_id"]),
        commit_sha=commit_sha,
        last_seen_epoch=float(winner.get("last_seen") or 0),
        span_count=int(winner.get("spans") or 0),
        sibling_run_ids=tuple(str(r["eval.run_id"]) for r in ordered[1:]),
    )


# --- run id -> RunSummary --------------------------------------------------


def load_run(
    client: SigNozClient,
    run_id: str,
    commit_sha: str,
    *,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    prefer_typed: bool = True,
) -> tuple[RunSummary, list[str]]:
    """Read one run back out of SigNoz as a `RunSummary`.

    Prefers M2's typed reader (`query.run_summary_typed`) when it exists --
    that one also carries trace ids, which M4's per-case deep links need. The
    fallback below reconstructs the same shape from the scalar builder queries
    M1 shipped, so the gate works standalone; it returns a note saying which
    path was taken, because "trace links are missing" should be visible in the
    report rather than mysterious.
    """
    typed = _typed_reader(client) if prefer_typed else None
    if typed is not None:
        summary = typed(run_id, lookback_minutes)
        if summary is not None and summary.cases:
            if not summary.commit_sha:
                summary.commit_sha = commit_sha
            return summary, []

    summary = _run_summary_fallback(
        client, run_id, commit_sha, lookback_minutes=lookback_minutes
    )
    return summary, [
        f"run `{run_id}`: read via the differ's own span aggregation "
        "(query.run_summary_typed unavailable); per-case trace links are omitted."
    ]


def _typed_reader(client: SigNozClient):
    """Bind M2's `run_summary_typed` however it ended up being exposed.

    Written before M2 landed, against a name agreed in the milestone contract
    but not a call signature -- so rather than guess wrong and crash the gate,
    try the shapes that make sense and fall through to the local reader.

    The lookback matters and is easy to lose: `resolve_run` may legitimately
    find a baseline run from yesterday, and a reader defaulting to a 60-minute
    window would then return zero cases for a run that plainly exists. Passing
    it through is the difference between a correct diff and "baseline run has
    no cases".
    """
    method = getattr(client, "run_summary_typed", None)
    if callable(method):

        def _bound(run_id: str, lookback: int) -> RunSummary | None:
            try:
                return method(run_id, lookback_minutes=lookback)
            except TypeError:
                return method(run_id)

        return _bound

    module_fn = getattr(query_mod, "run_summary_typed", None)
    if callable(module_fn):

        def _module(run_id: str, lookback: int) -> RunSummary | None:
            for first in (client, client.cfg):
                try:
                    return module_fn(first, run_id, lookback_minutes=lookback)
                except TypeError:
                    continue
            return None

        return _module

    return None


def _run_summary_fallback(
    client: SigNozClient,
    run_id: str,
    commit_sha: str,
    *,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
) -> RunSummary:
    """Rebuild a RunSummary from four scalar queries over the run's spans.

    Split by `preflight.span_role` because the interesting counts live on
    different span kinds: cost and tokens on `llm`, wall time on the `case`
    root, and the trajectory metrics on `tool` / `retrieval`.
    """
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - lookback_minutes * 60_000
    base = f"{_RUN_FIELD} = '{run_id}'"

    def by_case(aggs: list[Aggregation], extra: str = "") -> dict[str, dict[str, Any]]:
        rows = client.scalar(
            aggregations=aggs,
            filter_expression=base + extra,
            group_by=["eval.case_id"],
            start_ms=start_ms,
            end_ms=end_ms,
        )
        return {
            str(r["eval.case_id"]): r for r in rows if r.get("eval.case_id")
        }

    totals = by_case(
        [
            Aggregation("count()", "spans"),
            Aggregation("sum(gen_ai.usage.input_tokens)", "input_tokens"),
            Aggregation("sum(gen_ai.usage.output_tokens)", "output_tokens"),
            Aggregation("sum(preflight.cost_usd)", "cost_usd"),
            Aggregation("sum(preflight.duration_ms)", "duration_ms"),
            # 1/0 on the single case span, so the group sum is the verdict.
            # Same reading as query.run_summary_typed -- the two paths must not
            # disagree about whether a case passed.
            Aggregation("sum(preflight.success)", "success"),
        ]
    )
    tools = by_case([Aggregation("count()", "n")], f" AND {_ROLE_FIELD} = 'tool'")
    hops = by_case([Aggregation("count()", "n")], f" AND {_ROLE_FIELD} = 'retrieval'")

    cases = [
        CaseSummary(
            case_id=case_id,
            spans=int(row.get("spans") or 0),
            input_tokens=int(row.get("input_tokens") or 0),
            output_tokens=int(row.get("output_tokens") or 0),
            cost_usd=float(row.get("cost_usd") or 0.0),
            duration_ms=float(row.get("duration_ms") or 0.0),
            tool_calls=int((tools.get(case_id) or {}).get("n") or 0),
            retrieval_hops=int((hops.get(case_id) or {}).get("n") or 0),
            success=float(row.get("success") or 0.0) >= 1.0,
        )
        for case_id, row in sorted(totals.items())
    ]
    return RunSummary(run_id=run_id, commit_sha=commit_sha, cases=cases)


# --- comparison ------------------------------------------------------------


def threshold_for(cfg: Config, key: str) -> float:
    """Threshold for one metric, from preflight.yaml `thresholds:`."""
    raw = (cfg.thresholds or {}).get(THRESHOLD_KEYS[key])
    if raw is None:
        return FALLBACK_THRESHOLDS[key]
    return float(raw)


def noise_floor(cfg: Config, key: str) -> float:
    """Minimum absolute rise before a percentage breach is believed."""
    if key not in FLOOR_KEYS:
        return 0.0
    raw = (cfg.thresholds or {}).get(FLOOR_KEYS[key])
    return FALLBACK_FLOORS[key] if raw is None else float(raw)


def is_breach(
    key: str,
    baseline: float,
    candidate: float,
    threshold: float,
    *,
    floor: float = 0.0,
) -> bool:
    """Does this metric movement cross the gate?

    `success_rate` breaches on an absolute *drop*; everything in
    `HIGHER_IS_WORSE` breaches on a percentage *rise*. Improvements never
    breach -- the gate exists to catch regressions, not to enforce stability.
    """
    if key not in HIGHER_IS_WORSE:
        # success_rate and anything else added later that is higher-is-better.
        return (baseline - candidate) > threshold + _EPS

    rise = candidate - baseline
    if rise <= floor + _EPS:
        return False

    if baseline <= 0:
        # No division to do. Any movement away from zero is by definition an
        # infinite percentage rise, so gate on the movement itself.
        return True

    return (rise / baseline * 100.0) > threshold + _EPS


def _intersect_cases(
    baseline: RunSummary, candidate: RunSummary
) -> tuple[RunSummary, RunSummary, list[str]]:
    """Restrict both runs to the cases they have in common.

    Comparing a 6-case run against an 8-case run on per-task averages is not
    wrong so much as meaningless: the suite changed, so the denominators are
    not the same population. Compare the intersection and say so.
    """
    b_ids = {c.case_id for c in baseline.cases}
    c_ids = {c.case_id for c in candidate.cases}
    if b_ids == c_ids:
        return baseline, candidate, []

    shared = b_ids & c_ids
    if not shared:
        raise DiffError(
            f"runs {baseline.run_id} and {candidate.run_id} share no cases "
            f"(baseline: {sorted(b_ids) or '<none>'}; candidate: {sorted(c_ids) or '<none>'}). "
            "There is nothing comparable here -- refusing to emit a verdict."
        )

    notes = [
        f"Case sets differ; compared the {len(shared)} case(s) in common "
        f"({', '.join(sorted(shared))})."
    ]
    if only_b := sorted(b_ids - shared):
        notes.append(f"Baseline-only cases excluded: {', '.join(only_b)}.")
    if only_c := sorted(c_ids - shared):
        notes.append(f"Candidate-only cases excluded: {', '.join(only_c)}.")

    return (
        RunSummary(
            run_id=baseline.run_id,
            commit_sha=baseline.commit_sha,
            cases=[c for c in baseline.cases if c.case_id in shared],
        ),
        RunSummary(
            run_id=candidate.run_id,
            commit_sha=candidate.commit_sha,
            cases=[c for c in candidate.cases if c.case_id in shared],
        ),
        notes,
    )


def compare(
    baseline: RunSummary,
    candidate: RunSummary,
    cfg: Config,
    *,
    baseline_sha: str = "",
    candidate_sha: str = "",
    notes: Iterable[str] = (),
    metrics: Iterable[str] = GATED_METRICS,
) -> DiffReport:
    """Pure comparison of two runs. No I/O -- everything above already happened.

    Split out from `diff()` so the gate can be exercised (and its behaviour on
    zero baselines, missing cases, and each threshold direction proved) without
    a SigNoz round trip.
    """
    if not baseline.cases:
        raise DiffError(f"baseline run {baseline.run_id} has no cases in SigNoz.")
    if not candidate.cases:
        raise DiffError(f"candidate run {candidate.run_id} has no cases in SigNoz.")

    base_run, cand_run, shape_notes = _intersect_cases(baseline, candidate)
    all_notes = [*notes, *shape_notes]

    deltas: list[MetricDelta] = []
    for key in metrics:
        b = base_run.metric(key)
        c = cand_run.metric(key)
        threshold = threshold_for(cfg, key)
        floor = noise_floor(cfg, key)
        breached = is_breach(key, b, c, threshold, floor=floor)
        if floor and not breached and c > b and b > 0 and (c - b) / b * 100.0 > threshold:
            all_notes.append(
                f"`{key}`: rose {(c - b) / b * 100.0:+.1f}% but only {c - b:.3g}ms in "
                f"absolute terms, under the {floor:g}ms noise floor -- not gated."
            )
        if breached and key in HIGHER_IS_WORSE and b <= 0 and c > 0:
            all_notes.append(
                f"`{key}`: baseline is zero, so percentage change is undefined; "
                f"gated on the absolute rise to {c:.4g} instead."
            )
        deltas.append(
            MetricDelta(
                key=key,
                baseline=b,
                candidate=c,
                threshold=threshold,
                breached=breached,
                unit=UNITS.get(key, "count"),  # type: ignore[arg-type]
            )
        )

    if base_run.case_count < 3:
        all_notes.append(
            f"Only {base_run.case_count} case(s) compared -- p95 latency is the "
            "max of a very small sample; read it as a smoke signal, not a percentile."
        )

    return DiffReport(
        baseline_sha=baseline_sha or baseline.commit_sha,
        candidate_sha=candidate_sha or candidate.commit_sha,
        baseline_run_id=base_run.run_id,
        candidate_run_id=cand_run.run_id,
        deltas=deltas,
        baseline=base_run,
        candidate=cand_run,
        notes=all_notes,
    )


# --- the entry point -------------------------------------------------------


def diff(
    baseline_sha: str,
    candidate_sha: str,
    cfg: Config,
    *,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
) -> DiffReport:
    """Compare two commits' most recent suite runs. Raises DiffError if it can't.

    A `DiffError` is emphatically *not* a regression verdict: the caller must
    keep the two apart, or "the baseline run expired out of the lookback window"
    starts reading as "this PR broke the agent".
    """
    if baseline_sha == candidate_sha:
        raise DiffError(
            f"baseline and candidate are the same commit ({baseline_sha[:12]}); "
            "nothing to compare."
        )

    with SigNozClient(cfg) as client:
        base_ref = resolve_run(client, baseline_sha, lookback_minutes=lookback_minutes)
        cand_ref = resolve_run(client, candidate_sha, lookback_minutes=lookback_minutes)

        if base_ref.run_id == cand_ref.run_id:
            raise DiffError(
                f"both SHAs resolve to the same run {base_ref.run_id}; "
                "the candidate suite probably has not been run yet."
            )

        base_run, notes_b = load_run(
            client, base_ref.run_id, baseline_sha, lookback_minutes=lookback_minutes
        )
        cand_run, notes_c = load_run(
            client, cand_ref.run_id, candidate_sha, lookback_minutes=lookback_minutes
        )

    notes = [*notes_b, *notes_c]
    for ref, label in ((base_ref, "baseline"), (cand_ref, "candidate")):
        if ref.sibling_run_ids:
            notes.append(
                f"{label} `{ref.commit_sha[:8]}` has {len(ref.sibling_run_ids) + 1} runs; "
                f"used the most recent (`{ref.run_id}`)."
            )

    return compare(
        base_run,
        cand_run,
        cfg,
        baseline_sha=baseline_sha,
        candidate_sha=candidate_sha,
        notes=notes,
    )


# --- serialisation for M4 --------------------------------------------------


def to_dict(report: DiffReport) -> dict[str, Any]:
    """JSON-safe view of a DiffReport, including the computed properties.

    `dataclasses.asdict` drops `@property` values, and `delta` / `pct_change` /
    `breached` are exactly what a GitHub Action wants to branch on -- so this
    builds the dict by hand.
    """

    def case(c: CaseSummary) -> dict[str, Any]:
        return {
            "case_id": c.case_id,
            "spans": c.spans,
            "input_tokens": c.input_tokens,
            "output_tokens": c.output_tokens,
            "total_tokens": c.total_tokens,
            "cost_usd": c.cost_usd,
            "duration_ms": c.duration_ms,
            "tool_calls": c.tool_calls,
            "retrieval_hops": c.retrieval_hops,
            "success": c.success,
            "trace_id": c.trace_id,
        }

    def run(r: RunSummary | None) -> dict[str, Any] | None:
        if r is None:
            return None
        return {
            "run_id": r.run_id,
            "commit_sha": r.commit_sha,
            "case_count": r.case_count,
            "cases": [case(c) for c in r.cases],
        }

    return {
        "baseline_sha": report.baseline_sha,
        "candidate_sha": report.candidate_sha,
        "baseline_run_id": report.baseline_run_id,
        "candidate_run_id": report.candidate_run_id,
        "breached": report.breached,
        "exit_code": report.exit_code,
        "deltas": [
            {
                "key": d.key,
                "unit": d.unit,
                "baseline": d.baseline,
                "candidate": d.candidate,
                "delta": d.delta,
                "pct_change": (None if d.pct_change == float("inf") else d.pct_change),
                "threshold": d.threshold,
                "breached": d.breached,
                "higher_is_worse": d.key in HIGHER_IS_WORSE,
            }
            for d in report.deltas
        ],
        "notes": list(report.notes),
        "baseline": run(report.baseline),
        "candidate": run(report.candidate),
        "source": "signoz /api/v5/query_range",
    }


def breach_summary(report: DiffReport) -> str:
    """One line naming what broke -- for the CLI's stderr and CI's log."""
    breaches = [d for d in report.deltas if d.breached]
    if not breaches:
        return "no gated metric breached its threshold."
    parts = []
    for d in breaches:
        if d.key in HIGHER_IS_WORSE:
            pct = "inf" if d.pct_change == float("inf") else f"{d.pct_change:+.1f}%"
            parts.append(f"{d.key} {d.baseline:.4g} -> {d.candidate:.4g} ({pct}, threshold {d.threshold:g}%)")
        else:
            parts.append(
                f"{d.key} {d.baseline:.4g} -> {d.candidate:.4g} "
                f"(drop {d.baseline - d.candidate:.4g}, threshold {d.threshold:g})"
            )
    return "; ".join(parts)
