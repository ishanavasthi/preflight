"""Markdown rendering + SigNoz deep links for the PR comment.

M4 owns the real implementation. This stub exists so M3's differ can import and
call it against a fixed signature while M4 is still being built -- the two
milestones share this seam and nothing else.

Do not change these signatures without updating both callers.
"""

from __future__ import annotations

from preflight.contracts import DiffReport

# M4: replace by copying a real trace URL out of the SigNoz UI address bar.
# Per BUILD_PLAN.md this is deliberately not guessed -- a broken link in the PR
# comment is exactly what a judge clicks.
TRACE_URL_TEMPLATE = "{base_url}/trace/{trace_id}"


def trace_url(base_url: str, trace_id: str) -> str:
    """Deep link to one trace in SigNoz."""
    return TRACE_URL_TEMPLATE.format(base_url=base_url.rstrip("/"), trace_id=trace_id)


def render_markdown(report: DiffReport, *, signoz_url: str = "") -> str:
    """Render the gate verdict as a PR-comment-ready markdown document.

    Stub: a correct-but-plain table. M4 adds per-case deep links, the pass/fail
    banner, and the collapsed detail section.
    """
    verdict = "regression detected" if report.breached else "no regression"
    lines = [
        f"## Preflight — {verdict}",
        "",
        f"`{report.baseline_sha[:8]}` (baseline) → `{report.candidate_sha[:8]}` (candidate)",
        "",
        "| metric | baseline | candidate | change | threshold | |",
        "|---|---:|---:|---:|---:|:--:|",
    ]
    for d in report.deltas:
        mark = "❌" if d.breached else "✅"
        pct = "—" if d.pct_change in (0.0, float("inf")) else f"{d.pct_change:+.1f}%"
        lines.append(
            f"| {d.key} | {d.baseline:.4g} | {d.candidate:.4g} | {pct} | {d.threshold:g} | {mark} |"
        )
    if report.notes:
        lines += ["", *(f"> {n}" for n in report.notes)]
    lines += ["", "_Every number above came from SigNoz `/api/v5/query_range`._"]
    return "\n".join(lines)
