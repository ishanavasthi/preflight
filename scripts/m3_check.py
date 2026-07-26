#!/usr/bin/env python
"""M3 acceptance check: prove the gate fires, and prove it stays quiet.

BUILD_PLAN.md asks for "on the seeded regression branch -> exit 1 with the cost
delta named; on baseline -> exit 0". This script proves exactly that in two
layers:

  A. **Offline.** `differ.compare()` over synthetic RunSummary fixtures. No
     SigNoz, no network, no model calls -- so the threshold logic, the
     zero-baseline case, the differing-case-set case, and the success-rate
     direction are all exercised deterministically.

  B. **End to end.** Emits three real runs of synthetic spans into SigNoz under
     three fake commit SHAs -- a baseline, an identical re-run, and one with
     3x the tokens and an extra retrieval hop -- waits for ingest, then runs the
     real `differ.diff()` against the real query API both ways.

Layer B costs nothing: the spans are hand-emitted through `preflight.instrument`
with fixed token counts, so no LLM is called and the $1 project budget is
untouched.

    uv run python scripts/m3_check.py

Exit 0 means the gate behaves. Anything else means it does not.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from preflight import config as config_mod  # noqa: E402
from preflight import differ, instrument, otel  # noqa: E402
from preflight.contracts import CaseSummary, RunSummary  # noqa: E402
from preflight.query import SigNozClient, SigNozError  # noqa: E402
from preflight.runner import wait_for_ingest  # noqa: E402

CASES = ["refund-policy", "order-status", "backorder", "exchange", "late-refund", "damaged-item"]
MODEL = "fake-model-m1"

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        _failures.append(label)


# --- layer A: offline fixtures --------------------------------------------


def synth_run(run_id: str, sha: str, *, scale: float = 1.0, hops: int = 1,
              tools: int = 2, failures: int = 0, cases: list[str] | None = None) -> RunSummary:
    ids = cases or CASES
    return RunSummary(
        run_id=run_id,
        commit_sha=sha,
        cases=[
            CaseSummary(
                case_id=cid,
                spans=2 + hops + tools,
                input_tokens=int(600 * scale),
                output_tokens=int(120 * scale),
                cost_usd=round(0.0024 * scale, 8),
                duration_ms=180.0 + 10 * i,
                tool_calls=tools,
                retrieval_hops=hops,
                success=i >= failures,
            )
            for i, cid in enumerate(ids)
        ],
    )


def layer_a(cfg) -> None:
    print("\nA. offline threshold logic (synthetic RunSummary fixtures)")

    base = synth_run("run-base", "aaaa1111")

    clean = synth_run("run-clean", "bbbb2222", scale=1.05)  # +5%, inside every threshold
    rpt = differ.compare(base, clean, cfg, baseline_sha="aaaa1111", candidate_sha="bbbb2222")
    check("+5% across the board passes", rpt.exit_code == 0, differ.breach_summary(rpt))

    regressed = synth_run("run-bad", "cccc3333", scale=3.0, hops=2, tools=3)
    rpt = differ.compare(base, regressed, cfg, baseline_sha="aaaa1111", candidate_sha="cccc3333")
    named = {d.key for d in rpt.deltas if d.breached}
    check("3x tokens + extra hop fails", rpt.exit_code == 1, differ.breach_summary(rpt))
    check("cost_usd_per_task is named", "cost_usd_per_task" in named, ", ".join(sorted(named)))
    check("retrieval_hops_per_task is named", "retrieval_hops_per_task" in named)

    improved = synth_run("run-good", "dddd4444", scale=0.4)
    rpt = differ.compare(base, improved, cfg, baseline_sha="aaaa1111", candidate_sha="dddd4444")
    check("a 60% cost drop does not breach", rpt.exit_code == 0, differ.breach_summary(rpt))

    one_fail = synth_run("run-fail", "eeee5555", failures=1)
    rpt = differ.compare(base, one_fail, cfg)
    fail_rows = {d.key for d in rpt.deltas if d.breached}
    check("one newly failing case breaches success_rate", fail_rows == {"success_rate"}, str(fail_rows))

    # Zero baseline: nothing to divide by, so gate on the absolute rise.
    zero_hops = synth_run("run-z0", "ffff6666", hops=0)
    some_hops = synth_run("run-z1", "99997777", hops=2)
    rpt = differ.compare(zero_hops, some_hops, cfg)
    hop = next(d for d in rpt.deltas if d.key == "retrieval_hops_per_task")
    check("zero baseline -> breach without ZeroDivisionError", hop.breached)
    check("zero baseline is noted", any("baseline is zero" in n for n in rpt.notes))

    rpt = differ.compare(zero_hops, synth_run("run-z2", "8888", hops=0), cfg)
    check("zero baseline, zero candidate -> no breach", rpt.exit_code == 0)

    # Case sets differ: compare the intersection, say so.
    short = synth_run("run-short", "7777", cases=CASES[:4])
    rpt = differ.compare(base, short, cfg)
    check("differing case sets compare the intersection", rpt.candidate.case_count == 4)
    check("...and say so in notes", any("Case sets differ" in n for n in rpt.notes))
    check("...and name what was dropped", any("Baseline-only cases excluded" in n for n in rpt.notes))

    try:
        differ.compare(base, synth_run("run-alien", "6666", cases=["nothing-in-common"]), cfg)
        check("disjoint case sets refuse to emit a verdict", False)
    except differ.DiffError:
        check("disjoint case sets refuse to emit a verdict", True)

    try:
        differ.compare(base, RunSummary(run_id="empty", commit_sha="5555"), cfg)
        check("an empty candidate run raises rather than passing", False)
    except differ.DiffError:
        check("an empty candidate run raises rather than passing", True)


# --- layer B: real spans through real SigNoz -------------------------------


def emit_run(cfg, sha: str, *, scale: float, hops: int, tools: int) -> tuple[str, int]:
    """Write one synthetic suite run into SigNoz. No model is called.

    Note the single provider lifetime in `layer_b`: OpenTelemetry's global
    tracer provider can only be set once per process, so `otel.setup()` after an
    `otel.shutdown()` silently keeps returning the dead provider and every
    subsequent span is dropped. The SHA the gate reads is the *span* attribute
    stamped by `RunContext`, not the resource attribute, so emitting several
    commits' worth of runs under one provider is correct.
    """
    run_id = f"m3check-{uuid.uuid4().hex[:10]}"
    ctx = instrument.RunContext(run_id=run_id, commit_sha=sha)

    for i, case_id in enumerate(CASES):
        with instrument.case_span(ctx, case_id):
            for h in range(hops):
                with instrument.retrieval_span(ctx, name=f"kb-{h}", query=case_id):
                    pass
            with instrument.llm_span(ctx, cfg, model=MODEL) as result:
                result.input_tokens = int((600 + 10 * i) * scale)
                result.output_tokens = int((120 + 5 * i) * scale)
            for t in range(tools):
                with instrument.tool_span(ctx, name=f"tool-{t}", call_id=f"{case_id}-{t}"):
                    pass

    otel.force_flush()
    return run_id, len(CASES) * (2 + hops + tools)


def layer_b(cfg) -> None:
    print("\nB. end to end through SigNoz /api/v5/query_range")

    if not cfg.signoz_api_key:
        print("  [skip] SIGNOZ_API_KEY unset -- source .env first.")
        _failures.append("layer B skipped (no API key)")
        return

    tag = uuid.uuid4().hex[:8]
    base_sha = f"m3base{tag}"
    clean_sha = f"m3clean{tag}"
    bad_sha = f"m3bad{tag}"

    plan = [
        (base_sha, dict(scale=1.0, hops=1, tools=2)),
        (clean_sha, dict(scale=1.04, hops=1, tools=2)),   # noise-sized drift
        (bad_sha, dict(scale=3.0, hops=2, tools=3)),      # the seeded regression shape
    ]
    otel.setup(cfg, commit_sha=base_sha)
    try:
        for sha, kwargs in plan:
            run_id, expected = emit_run(cfg, sha, **kwargs)
            print(f"  emitted {run_id} ({expected} spans) for {sha}")
            observed = wait_for_ingest(cfg, run_id, expected)
            print(f"    {observed}/{expected} spans visible in SigNoz")
    finally:
        otel.shutdown()

    # Prove SHA -> most-recent-run resolution works against the live API.
    with SigNozClient(cfg) as client:
        ref = differ.resolve_run(client, bad_sha, lookback_minutes=60)
    check("resolved a SHA to a run id via SigNoz", bool(ref.run_id), ref.run_id)

    rpt = differ.diff(base_sha, clean_sha, cfg, lookback_minutes=60)
    print("\n" + "-" * 70)
    print(f"$ preflight diff --baseline {base_sha} --candidate {clean_sha}")
    print(_render(cfg, rpt))
    print(f"exit {rpt.exit_code}")
    print("-" * 70)
    check("baseline vs clean re-run exits 0", rpt.exit_code == 0, differ.breach_summary(rpt))

    rpt = differ.diff(base_sha, bad_sha, cfg, lookback_minutes=60)
    print("\n" + "-" * 70)
    print(f"$ preflight diff --baseline {base_sha} --candidate {bad_sha}")
    print(_render(cfg, rpt))
    print(f"exit {rpt.exit_code}")
    print("-" * 70)
    check("baseline vs regression exits 1", rpt.exit_code == 1, differ.breach_summary(rpt))
    cost = next(d for d in rpt.deltas if d.key == "cost_usd_per_task")
    check(
        "the cost delta is named",
        cost.breached,
        f"{cost.baseline:.6f} -> {cost.candidate:.6f} ({cost.pct_change:+.1f}%)",
    )


def _render(cfg, rpt) -> str:
    from preflight import report as report_mod

    return report_mod.render_markdown(rpt, signoz_url=cfg.signoz_url)


def main() -> int:
    cfg = config_mod.load()
    print("M3 gate check -- preflight/differ.py")
    layer_a(cfg)
    if os.getenv("PREFLIGHT_SKIP_E2E"):
        print("\nB. skipped (PREFLIGHT_SKIP_E2E set)")
    else:
        try:
            layer_b(cfg)
        except SigNozError as exc:
            print(f"  [FAIL] SigNoz unreachable: {exc}")
            _failures.append("layer B: SigNoz unreachable")

    print("")
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
