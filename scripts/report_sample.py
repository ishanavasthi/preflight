#!/usr/bin/env python3
"""Render a sample PR comment, and verify every SigNoz link in it resolves.

Two jobs, neither of which needs the differ or a live suite run:

    python scripts/report_sample.py             # render the failing + passing comment
    python scripts/report_sample.py --verify    # ...and check every link against SigNoz

`--verify` is the part that matters. It pulls real `trace_id`s out of SigNoz,
builds the deep links exactly the way `report.py` does, and then asks the API
that backs the trace-detail page -- `POST /api/v4/traces/{id}/waterfall` --
whether each one actually resolves. A link that 404s fails the script.

That is the check `BUILD_PLAN.md` asks for ("do not guess it into report.py")
made repeatable, so it can be re-run after any SigNoz upgrade rather than
trusted forever on one manual copy out of the address bar.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from preflight import config as config_mod  # noqa: E402
from preflight import report as report_mod  # noqa: E402
from preflight.contracts import (  # noqa: E402
    CaseSummary,
    DiffReport,
    MetricDelta,
    RunSummary,
)

BASELINE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
CANDIDATE_SHA = "f0e9d8c7b6a5948372615f4e3d2c1b0a98765432"

CASES = [
    # case_id, baseline (tok_in, tok_out, ms, tools, hops), candidate (same)
    ("01-refund-policy", (1180, 240, 1820, 2, 1), (3410, 690, 4310, 5, 2)),
    ("02-order-status", (940, 180, 1310, 2, 1), (2680, 520, 3120, 4, 2)),
    ("03-backorder", (1320, 260, 2040, 3, 1), (3980, 810, 5220, 6, 3)),
    ("04-exchange", (1010, 200, 1490, 2, 1), (2910, 570, 3480, 5, 2)),
    ("05-late-refund", (1240, 250, 1930, 3, 1), (3620, 730, 4680, 5, 2)),
    ("06-damaged-item", (1090, 210, 1610, 2, 1), (3150, 600, 3790, 4, 2)),
]

PRICE_IN = 2.00 / 1_000_000
PRICE_OUT = 10.00 / 1_000_000


def _case(case_id: str, spec, trace_id: str, success: bool = True) -> CaseSummary:
    tok_in, tok_out, ms, tools, hops = spec
    return CaseSummary(
        case_id=case_id,
        spans=5 + tools,
        input_tokens=tok_in,
        output_tokens=tok_out,
        cost_usd=tok_in * PRICE_IN + tok_out * PRICE_OUT,
        duration_ms=float(ms),
        tool_calls=tools,
        retrieval_hops=hops,
        success=success,
        trace_id=trace_id,
    )


def _delta(key: str, base: RunSummary, cand: RunSummary, threshold: float, unit: str) -> MetricDelta:
    b, c = base.metric(key), cand.metric(key)
    if unit == "ratio":
        breached = (b - c) > threshold
    else:
        breached = b > 0 and (c - b) / b * 100.0 > threshold
    return MetricDelta(
        key=key, baseline=b, candidate=c, threshold=threshold, breached=breached, unit=unit
    )


THRESHOLDS = [
    ("cost_usd_per_task", 20.0, "usd"),
    ("total_tokens_per_task", 20.0, "tokens"),
    ("p95_latency_ms", 50.0, "ms"),
    ("tool_calls_per_task", 30.0, "count"),
    ("retrieval_hops_per_task", 30.0, "count"),
    ("success_rate", 0.0, "ratio"),
]


def build_report(*, regressed: bool, trace_ids: dict[str, str]) -> DiffReport:
    baseline = RunSummary(
        run_id="run-baseline-9f21c4",
        commit_sha=BASELINE_SHA,
        cases=[_case(cid, b, trace_ids.get(cid, "")) for cid, b, _ in CASES],
    )
    candidate = RunSummary(
        run_id="run-candidate-4d88ae",
        commit_sha=CANDIDATE_SHA,
        cases=[
            _case(cid, (c if regressed else b), trace_ids.get(cid, ""))
            for cid, b, c in CASES
        ],
    )
    deltas = [_delta(k, baseline, candidate, t, u) for k, t, u in THRESHOLDS]
    notes = [
        "Suite ran in replay mode (`PREFLIGHT_REPLAY=1`) — cassettes are committed, "
        "so both runs are byte-identical apart from the prompt change.",
        f"Baseline resolved from `git merge-base` at `{BASELINE_SHA[:8]}`.",
    ]
    return DiffReport(
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        baseline_run_id=baseline.run_id,
        candidate_run_id=candidate.run_id,
        deltas=deltas,
        baseline=baseline,
        candidate=candidate,
        notes=notes,
    )


# --- real trace ids -------------------------------------------------------


def fetch_trace_ids(cfg, limit: int) -> dict[str, str]:
    """Pull real trace ids out of SigNoz so the sample links are clickable."""
    key = cfg.signoz_api_key
    if not key:
        return {}
    end_ms = int(time.time() * 1000)
    body = {
        "schemaVersion": "v1",
        "start": end_ms - 24 * 60 * 60_000,
        "end": end_ms,
        "requestType": "raw",
        "compositeQuery": {
            "queries": [
                {
                    "type": "builder_query",
                    "spec": {
                        "name": "A",
                        "signal": "traces",
                        "filter": {"expression": "eval.run_id EXISTS"},
                        "selectFields": [{"name": "trace_id"}],
                        "limit": 200,
                    },
                }
            ]
        },
    }
    try:
        resp = httpx.post(
            f"{cfg.signoz_url}/api/v5/query_range",
            headers={"SIGNOZ-API-KEY": key, "Content-Type": "application/json"},
            json=body,
            timeout=30.0,
        )
        resp.raise_for_status()
        rows = resp.json()["data"]["data"]["results"][0]["rows"]
    except Exception as exc:  # noqa: BLE001 - sample script, report and move on
        print(f"  (could not fetch real trace ids: {exc})", file=sys.stderr)
        return {}

    seen: list[str] = []
    for r in rows:
        tid = str(r.get("data", {}).get("trace_id") or "")
        if tid and tid not in seen:
            seen.append(tid)
    return {cid: seen[i % len(seen)] for i, (cid, _, _) in enumerate(CASES[:limit])} if seen else {}


# --- link verification ----------------------------------------------------

LINK_RE = re.compile(r"\]\((https?://[^)]+)\)")


def verify_links(markdown: str, cfg) -> int:
    """Every /trace/<id> link in the comment must resolve to a real trace."""
    urls = sorted(set(LINK_RE.findall(markdown)))
    if not urls:
        print("no links in the rendered comment", file=sys.stderr)
        return 1

    key = cfg.signoz_api_key
    if not key:
        print("SIGNOZ_API_KEY not set; cannot verify", file=sys.stderr)
        return 1

    failures = 0
    print(f"\nverifying {len(urls)} link(s) against {cfg.signoz_url}\n")
    for url in urls:
        m = re.search(r"/trace/([0-9a-fA-F]+)(?:\?spanId=([0-9a-fA-F]+))?", url)
        if not m:
            print(f"  SKIP  {url}  (not a trace link)")
            continue
        trace_id, span_id = m.group(1), m.group(2)

        # 1. the SPA route serves the app
        route = httpx.get(url, timeout=30.0)
        # 2. the API behind the trace-detail page actually has this trace
        api = httpx.post(
            f"{cfg.signoz_url}/api/v4/traces/{trace_id}/waterfall",
            headers={"SIGNOZ-API-KEY": key, "Content-Type": "application/json"},
            json={},
            timeout=30.0,
        )
        payload = api.json() if api.status_code == 200 else {}
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        spans = data.get("spans") or []
        ok = route.status_code == 200 and api.status_code == 200 and bool(spans)

        span_ok = True
        if span_id:
            ids = {s.get("spanId") or s.get("span_id") for s in spans}
            span_ok = span_id in ids

        status = "OK  " if (ok and span_ok) else "FAIL"
        failures += 0 if (ok and span_ok) else 1
        print(
            f"  {status}  route={route.status_code} waterfall={api.status_code} "
            f"spans={len(spans)}"
            + (f" span_present={span_ok}" if span_id else "")
        )
        print(f"        {url}")

    print()
    if failures:
        print(f"{failures} link(s) did not resolve", file=sys.stderr)
    else:
        print(f"all {len(urls)} link(s) resolve")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="Check every link against SigNoz.")
    ap.add_argument("--pass", dest="passing", action="store_true", help="Render the green comment.")
    ap.add_argument("--output", help="Write the markdown here instead of stdout.")
    args = ap.parse_args()

    cfg = config_mod.load()
    trace_ids = fetch_trace_ids(cfg, len(CASES))
    if not trace_ids:
        print(
            "  (no traces in SigNoz — rendering without deep links)",
            file=sys.stderr,
        )

    report = build_report(regressed=not args.passing, trace_ids=trace_ids)
    markdown = report_mod.render_markdown(report, signoz_url=cfg.signoz_url)

    if args.output:
        Path(args.output).write_text(markdown)
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(markdown)

    print(
        f"\n[gate would exit {report.exit_code}]",
        file=sys.stderr,
    )

    if args.verify:
        return verify_links(markdown, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
