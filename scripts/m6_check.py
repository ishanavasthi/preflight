"""BUILD_PLAN's M6 acceptance check, automated.

    "Force a failure; the explanation names the specific case and metric, and
     the agent's own investigation shows up as a trace in SigNoz."

Both halves are asserted, and the second half is asserted *through the MCP
server* -- the diagnosis agent's trace is read back with the same
`signoz_get_trace_details` the agent itself used. That is the full circle the
milestone is about: the tool that explains the telemetry is visible in the
telemetry, and the same interface proves it.

The interesting assertion is #3. The gate's own per-case table is in the
agent's prompt, so "names the worst case" is a weak test -- the model could
parrot it. `policy_search` and `policy-kb` appear **nowhere in the prompt**;
they exist only in span attributes. Requiring one of them in the explanation is
what separates a grounded diagnosis from a fluent paraphrase.

Usage
-----
    python scripts/m6_check.py
    python scripts/m6_check.py --baseline <sha> --candidate <sha>
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from preflight import config, diagnose, mcp  # noqa: E402
from preflight.differ import breach_summary, diff  # noqa: E402

# The demo PR: `seeded-regression` against the commit it branched from.
BASELINE = "e0592cf84cc63ee4f3a6c1d0435b42d48df52728"
CANDIDATE = "59607e52008b29a41f9722671f3e7a4f61914b61"

# The query window `.cassettes-diagnose/` was recorded against, epoch ms.
# Pinning it is what makes this check reproducible: the window appears in every
# MCP call's arguments, so it is part of the cassette key, and an unpinned
# window silently misses every cassette ten minutes after recording.
#
# The check therefore replays by default -- it runs to completion with no
# ANTHROPIC_API_KEY at all, which is the state a judge cloning this repo is in.
# `--live` re-runs the investigation for real against a fresh window; that is
# how the committed cassettes were made, and it costs about $0.06.
RECORDED_WINDOW_END_MS = 1785133200000

# Ground truth for the seeded regression, from the diff of agent/reference.py
# and the spans it produced. Every one of these is checked case-insensitively.
WORST_CASE = "damaged-item"
METRIC_WORDS = ("cost", "token")
# Span-level evidence: a tool and a retrieval source that only exist in SigNoz.
SPAN_EVIDENCE = ("policy_search", "policy-kb", "policy search")

PASS = "  PASS"
FAIL = "  FAIL"


def _check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"{PASS if ok else FAIL}  {label}" + (f"  --  {detail}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", default=BASELINE)
    ap.add_argument("--candidate", default=CANDIDATE)
    ap.add_argument("--lookback-minutes", type=int, default=24 * 60)
    ap.add_argument(
        "--live",
        action="store_true",
        help="call the API for a fresh investigation instead of replaying "
        "the committed cassettes (~$0.06)",
    )
    args = ap.parse_args()

    if not args.live:
        # Fail loudly on a cassette miss rather than quietly spending money:
        # a check that can bill you is a check people stop running.
        os.environ["PREFLIGHT_REPLAY"] = "1"

    cfg = config.load()
    failures = 0

    # -- [1/4] force a failure -------------------------------------------
    print(f"[1/4] gate the seeded regression "
          f"({args.baseline[:8]} -> {args.candidate[:8]})")
    report = diff(args.baseline, args.candidate, cfg,
                  lookback_minutes=args.lookback_minutes)
    print(f"       exit_code={report.exit_code}  {breach_summary(report)[:150]}")
    if not _check(report.breached, "the gate failed, so there is something to explain"):
        return 1

    # -- [2/4] diagnose over MCP ------------------------------------------
    print("\n[2/4] diagnosis agent, driving the SigNoz MCP server")
    started = time.perf_counter()
    try:
        result = diagnose.diagnose(
            report,
            cfg,
            lookback_minutes=args.lookback_minutes,
            window_end_ms=None if args.live else RECORDED_WINDOW_END_MS,
        )
    except diagnose.DiagnoseError as exc:
        print(f"{FAIL}  the diagnosis agent could not run\n\n{exc}\n")
        return 1
    print(f"       {result.summary()}  in {time.perf_counter() - started:.1f}s")
    print(f"       mcp calls: {', '.join(result.tool_calls) or 'NONE'}")
    print("\n" + "-" * 72)
    print(result.text)
    print("-" * 72 + "\n")

    # -- [3/4] the explanation is specific, and grounded -------------------
    print("[3/4] the explanation names the case, the metric, and span evidence")
    low = result.text.lower()
    failures += not _check(WORST_CASE in low, f"names the worst case ({WORST_CASE})")
    hit = [w for w in METRIC_WORDS if w in low]
    failures += not _check(bool(hit), "names the regressed metric", ", ".join(hit))
    evidence = [w for w in SPAN_EVIDENCE if w in low]
    failures += not _check(
        bool(evidence),
        "cites span-level evidence absent from its own prompt",
        ", ".join(evidence) or "none of " + "/".join(SPAN_EVIDENCE),
    )
    failures += not _check(
        len(result.tool_calls) >= 1,
        "the investigation actually went through MCP",
        f"{len(result.tool_calls)} tool call(s)",
    )

    # -- [4/4] full circle: read the agent's own trace back ----------------
    print("\n[4/4] the investigation is itself a trace in SigNoz")
    print(f"       trace_id {result.trace_id}")
    spans: list[dict] = []
    # The collector restarts shortly after boot (opamp config sync) and drops
    # what was exported during that window, so an empty read here is not
    # automatically a broken exporter -- poll before believing it.
    deadline = time.time() + cfg.poll_timeout_seconds
    end_ms = int(time.time() * 1000) + 60_000
    start_ms = end_ms - 3 * 3_600_000
    with mcp.MCPClient(client_name="preflight-m6-check") as client:
        while time.time() < deadline:
            payload = client.call(
                "signoz_get_trace_details",
                {
                    "traceId": result.trace_id,
                    "start": start_ms,
                    "end": end_ms,
                    "searchContext": "M6 acceptance check: read the diagnosis "
                                     "agent's own investigation trace back out "
                                     "of SigNoz through MCP",
                },
            )
            compacted = mcp.compact(payload)
            spans = (compacted or {}).get("spans", []) if isinstance(compacted, dict) else []
            # Poll for the ROOT span, not for any span. The root closes last and
            # is exported last, so "the trace exists" goes true a beat before the
            # trace is complete -- breaking on the first non-empty read reports a
            # missing root that arrives 200ms later. Waiting on the last-written
            # span is the only condition that means the whole trace has landed.
            if any(s.get("name", "").startswith("diagnose") for s in spans):
                break
            time.sleep(cfg.poll_interval_seconds)

    names = sorted({s.get("name", "") for s in spans})
    print(f"       {len(spans)} span(s) ingested: {', '.join(names)[:200]}")
    failures += not _check(bool(spans), "the diagnosis trace is queryable in SigNoz")
    failures += not _check(
        any(n.startswith("diagnose") for n in names),
        "the root investigation span is present",
    )
    failures += not _check(
        any(n.startswith("chat ") for n in names),
        "the agent's own LLM turns are spans",
    )
    failures += not _check(
        any(n.startswith("execute_tool signoz_") for n in names),
        "each MCP call to SigNoz is a span",
    )

    link = f"{cfg.signoz_url}/trace/{result.trace_id}"
    print(f"\n       {link}")
    if failures:
        print(f"\nFAIL: {failures} assertion(s) failed")
        return 1
    print("\nPASS: the gate failed, the agent explained why from SigNoz, and its "
          "own\n      investigation is a trace in SigNoz.")
    print(f"      diagnosis trace: {result.trace_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
