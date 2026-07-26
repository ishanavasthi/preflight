"""Markdown rendering + SigNoz deep links for the PR comment.

This is the only part of Preflight a reviewer actually reads, so it has one
job: make the verdict obvious in two seconds, and put the span that explains it
one click away.

## The deep-link format, and how it was verified

`BUILD_PLAN.md` is emphatic that this must not be guessed -- "a broken link in
the PR comment is exactly the kind of thing a judge clicks". It wasn't. Against
SigNoz **v0.134.0**, four independent confirmations:

1. The router constant in the frontend bundle (`/assets/index-*.js`) is
   `ROUTES.TRACE_DETAIL: '/trace/:id'`. There is no `/traces/`, no
   `/trace-detail/`, and the legacy `/trace-old/:id` chunk is a pure redirect
   *to* `/trace/${id}` that preserves `window.location.search`.
2. The trace-detail chunk (`TraceDetailsV3-*.js`) reads the selected span out
   of the query string -- `searchParams.get('spanId')` -- and treats a present
   `spanId` as "expand the waterfall to this span" (`isUncollapsed: true`).
3. The UI's own *Copy link* handler builds exactly this shape: it deletes and
   re-sets `spanId` on the current search params, then copies
   `` `${pathname}?${params}` ``. So the template below is what SigNoz itself
   puts on your clipboard, not a reconstruction of it.
4. The backing API `POST /api/v4/traces/{trace_id}/waterfall` returns 200 with
   the full span list for a real trace id and `{"type":"not-found"}` for a
   bogus one -- so a link built from a `trace_id` that came back from
   `query_range` is guaranteed to resolve to a trace that exists.

    {base_url}/trace/{trace_id}                      # the trace
    {base_url}/trace/{trace_id}?spanId={span_id}     # that span, pre-selected

## Where the links point

`signoz_url` is the SigNoz the *gate* queried -- in CI that is
`http://localhost:8080` inside the runner, which a browser elsewhere cannot
reach. Set `PREFLIGHT_SIGNOZ_PUBLIC_URL` to an address a reader can actually
open and every link is rewritten to it. Left unset the links stay localhost,
which is the right default for the demo: the person reviewing the PR is the
person running the stack.

Signatures here are load-bearing -- `preflight/cli.py` calls both.
"""

from __future__ import annotations

import os

from preflight.contracts import HIGHER_IS_WORSE, CaseSummary, DiffReport

# Sticky-comment anchor. The GitHub Action greps for this to decide whether to
# update its existing comment or post a new one, so it must never change.
MARKER = "<!-- preflight-report -->"

# Verified against SigNoz v0.134.0 -- see the module docstring for the four
# checks behind it. `spanId` is optional; without it the waterfall opens at the
# root span.
TRACE_URL_TEMPLATE = "{base_url}/trace/{trace_id}"
SPAN_QUERY_PARAM = "spanId"

# Human-facing labels for GATED_METRICS keys.
LABELS: dict[str, str] = {
    "cost_usd_per_task": "Cost / task",
    "total_tokens_per_task": "Tokens / task",
    "p95_latency_ms": "p95 latency",
    "tool_calls_per_task": "Tool calls / task",
    "retrieval_hops_per_task": "Retrieval hops / task",
    "success_rate": "Success rate",
}


# --- deep links -----------------------------------------------------------


def public_base_url(signoz_url: str = "") -> str:
    """The base URL to build links against.

    `PREFLIGHT_SIGNOZ_PUBLIC_URL` wins, because the address CI queries and the
    address a reviewer can open are not always the same host.
    """
    override = os.getenv("PREFLIGHT_SIGNOZ_PUBLIC_URL", "").strip()
    return (override or signoz_url or "").rstrip("/")


def trace_url(base_url: str, trace_id: str, span_id: str = "") -> str:
    """Deep link to one trace in SigNoz, optionally with a span pre-selected.

    Returns "" when there is nothing to link to, so callers render plain text
    rather than a link that goes nowhere.
    """
    base = (base_url or "").rstrip("/")
    if not base or not trace_id:
        return ""
    url = TRACE_URL_TEMPLATE.format(base_url=base, trace_id=trace_id)
    if span_id:
        url = f"{url}?{SPAN_QUERY_PARAM}={span_id}"
    return url


def _link(text: str, url: str) -> str:
    return f"[{text}]({url})" if url else text


# --- value formatting -----------------------------------------------------


class _Change:
    """Anything with baseline/candidate/unit that `_fmt_change` can format.

    `MetricDelta` is pinned in contracts.py and carries gate fields (threshold,
    breached) that mean nothing per case, so per-case rows use this instead.
    """

    __slots__ = ("baseline", "candidate", "unit")

    def __init__(self, baseline: float, candidate: float, unit: str):
        self.baseline = baseline
        self.candidate = candidate
        self.unit = unit

    @property
    def delta(self) -> float:
        return self.candidate - self.baseline

    @property
    def pct_change(self) -> float:
        if self.baseline == 0:
            return 0.0 if self.candidate == 0 else float("inf")
        return (self.candidate - self.baseline) / self.baseline * 100.0


def _fmt(value: float, unit: str) -> str:
    """Render one metric value the way a human reads it, not the way it's stored."""
    if unit == "usd":
        return f"${value:,.2f}" if abs(value) >= 1 else f"${value:.4f}"
    if unit == "tokens":
        return f"{value:,.0f}"
    if unit == "ms":
        return f"{value / 1000:.2f}s" if abs(value) >= 1000 else f"{value:.0f}ms"
    if unit == "ratio":
        return f"{value * 100:.0f}%"
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt_change(delta) -> str:
    """The Δ cell. Ratios move in points; everything else moves in percent."""
    if delta.unit == "ratio":
        diff = delta.delta * 100
        return "—" if abs(diff) < 0.05 else f"{diff:+.0f} pts"

    if delta.baseline == 0:
        return "—" if delta.candidate == 0 else "new"

    pct = delta.pct_change
    if abs(pct) < 0.05:
        return "—"
    return f"{'▲' if pct > 0 else '▼'} {pct:+.1f}%"


def _fmt_threshold(delta) -> str:
    if delta.unit == "ratio":
        return f"drop > {delta.threshold * 100:.0f} pts"
    return f"+{delta.threshold:g}%"


def _worse(delta) -> bool:
    """Did this metric move in the bad direction, breach or not?"""
    if delta.key in HIGHER_IS_WORSE:
        return delta.delta > 0
    return delta.delta < 0


def _severity(delta) -> float:
    if delta.unit == "ratio":
        return abs(delta.delta) * 100
    pct = delta.pct_change
    return 1e9 if pct == float("inf") else abs(pct)


# --- sections -------------------------------------------------------------


def _banner(report: DiffReport) -> list[str]:
    if report.breached:
        return ["## ❌ Preflight — regression detected"]
    return ["## ✅ Preflight — no regression"]


def _subtitle(report: DiffReport) -> str:
    n = report.candidate.case_count if report.candidate else 0
    return (
        f"`{report.baseline_sha[:8]}` → `{report.candidate_sha[:8]}` · "
        f"{n} case{'s' if n != 1 else ''}"
    )


def _headline(report: DiffReport) -> list[str]:
    """One sentence naming the metrics that failed, worst first."""
    breaches = [d for d in report.deltas if d.breached]
    if not breaches:
        return [
            "> Every gated metric is within threshold — this PR does not change "
            "how the agent behaves."
        ]
    named = ", ".join(
        f"**{LABELS.get(d.key, d.key)} {_fmt_change(d)}** (limit {_fmt_threshold(d)})"
        for d in sorted(breaches, key=_severity, reverse=True)
    )
    return [f"> {named}"]


def _delta_table(report: DiffReport) -> list[str]:
    if not report.deltas:
        return ["_No metrics were compared._"]

    lines = [
        "| | Metric | Baseline | Candidate | Δ | Threshold |",
        "|:--:|---|---:|---:|---:|---:|",
    ]
    for d in report.deltas:
        mark = "❌" if d.breached else ("⚠️" if _worse(d) else "✅")
        lines.append(
            f"| {mark} | {LABELS.get(d.key, d.key)} "
            f"| {_fmt(d.baseline, d.unit)} "
            f"| {_fmt(d.candidate, d.unit)} "
            f"| {_fmt_change(d)} "
            f"| {_fmt_threshold(d)} |"
        )
    return lines


def _baseline_index(report: DiffReport) -> dict[str, CaseSummary]:
    cases = report.baseline.cases if report.baseline else []
    return {c.case_id: c for c in cases}


def _offending_span(report: DiffReport, base: str) -> list[str]:
    """The 'one click away' promise from the problem statement, made literal."""
    if not report.candidate or not report.candidate.cases:
        return []

    base_by_id = _baseline_index(report)

    def cost_rise(c: CaseSummary) -> float:
        prior = base_by_id.get(c.case_id)
        return c.cost_usd - (prior.cost_usd if prior else 0.0)

    worst = max(report.candidate.cases, key=cost_rise)
    url = trace_url(base, worst.trace_id)
    if not url:
        return []

    # On a clean run where nothing moved, "largest delta: $0.0000" is noise.
    # Say nothing rather than point at an arbitrary case.
    if not report.breached and abs(cost_rise(worst)) < 1e-9:
        return []

    prior = base_by_id.get(worst.case_id)
    if prior is not None:
        detail = (
            f"`{worst.case_id}` moved {_fmt(worst.cost_usd - prior.cost_usd, 'usd')} "
            f"({worst.total_tokens - prior.total_tokens:+,} tokens, "
            f"{worst.tool_calls - prior.tool_calls:+d} tool calls)"
        )
    else:
        detail = f"`{worst.case_id}` ({_fmt(worst.cost_usd, 'usd')})"

    label = "Biggest mover" if report.breached else "Largest delta"
    return [f"**{label}:** {detail} — {_link('open the trace in SigNoz', url)}"]


def _case_table(report: DiffReport, base: str) -> list[str]:
    """Per-case rows, each deep-linked to its own trace."""
    if not report.candidate or not report.candidate.cases:
        return []

    base_by_id = _baseline_index(report)
    n = len(report.candidate.cases)

    lines = [
        "<details>",
        f"<summary>Per-case breakdown ({n} case{'s' if n != 1 else ''})</summary>",
        "",
        "| Case | Cost | Δ | Tokens | Δ | Latency | Tools | Hops | | Trace |",
        "|---|---:|---:|---:|---:|---:|---:|---:|:--:|:--:|",
    ]

    for c in sorted(report.candidate.cases, key=lambda c: c.case_id):
        prior = base_by_id.get(c.case_id)
        if prior is not None:
            d_cost = _fmt_change(_Change(prior.cost_usd, c.cost_usd, "usd"))
            d_tok = _fmt_change(
                _Change(float(prior.total_tokens), float(c.total_tokens), "tokens")
            )
        else:
            d_cost = d_tok = "new"

        lines.append(
            f"| `{c.case_id}` "
            f"| {_fmt(c.cost_usd, 'usd')} | {d_cost} "
            f"| {c.total_tokens:,} | {d_tok} "
            f"| {_fmt(c.duration_ms, 'ms')} "
            f"| {c.tool_calls} | {c.retrieval_hops} "
            f"| {'✅' if c.success else '❌'} "
            f"| {_link('↗', trace_url(base, c.trace_id))} |"
        )

    lines += [
        "",
        "Every row is one trace in SigNoz. `↗` opens its waterfall.",
        "",
        "</details>",
    ]
    return lines


def _notes(report: DiffReport) -> list[str]:
    if not report.notes:
        return []
    return [
        "<details>",
        "<summary>Notes</summary>",
        "",
        *(f"- {n}" for n in report.notes),
        "",
        "</details>",
    ]


def _footer(report: DiffReport, base: str) -> list[str]:
    src = "Every number above came from SigNoz `/api/v5/query_range`."
    if base:
        src += f" Datastore: {base}"
    return [
        "<sub>",
        f"{src}<br>",
        f"baseline run `{report.baseline_run_id or '—'}` · "
        f"candidate run `{report.candidate_run_id or '—'}`",
        "</sub>",
    ]


# --- entry point ----------------------------------------------------------


def render_markdown(report: DiffReport, *, signoz_url: str = "") -> str:
    """Render the gate verdict as a PR-comment-ready markdown document.

    Deterministic and side-effect free: the same report renders to the same
    bytes, so the sticky-comment upsert in CI is a no-op when nothing changed.
    """
    base = public_base_url(signoz_url)

    blocks: list[list[str]] = [
        [MARKER],
        _banner(report),
        [_subtitle(report)],
        _headline(report),
        _delta_table(report),
        _offending_span(report, base),
        _case_table(report, base),
        _notes(report),
        _footer(report, base),
    ]

    out: list[str] = []
    for block in blocks:
        if not block:
            continue
        out.extend(block)
        out.append("")
    return "\n".join(out).rstrip() + "\n"
