"""The diagnosis agent: given a failed gate, ask SigNoz *why* and say it in English.

M3's differ answers "did the agent get worse". It does not answer "what did the
PR actually do", and that second question is the one a reviewer has at 2am. This
module closes the gap: it hands the failed `DiffReport` to a model, gives it the
SigNoz **MCP server** as its only source of facts, and lets it drive the
investigation -- aggregate spans by case, pull the trajectory of the worst one,
compare it against the baseline -- until it can name the case, the metric and
the likely cause.

Two properties make this more than a wrapper around a prompt:

**Every number in the explanation came out of SigNoz.** The model is given the
gate's verdict as context, but the tools are the only way it can learn anything
new, and both of them are MCP calls. It cannot read the repo. So an explanation
that names a specific case and a specific span is an explanation grounded in
telemetry rather than in a plausible story about prompts.

**The investigation is itself a trace.** The agent runs under
`preflight/instrument.py`, so its own LLM turns and its own MCP tool calls land
in SigNoz as spans, with token counts and cost on the same GenAI attributes the
reference agent uses. The tool that explains the observability data is visible
in the observability data -- which is the whole point of M6, and the reason
`--print-trace-id` exists.

The diagnosis spans are deliberately kept out of the gate's namespace. They
carry `vcs.commit_sha = diagnosis-<sha>` rather than the real SHA, and run under
service name `preflight-diagnose`, so `differ.resolve_run` can never mistake a
diagnosis for a suite run and diff against it. That is not a stylistic choice:
the differ resolves a SHA to *the most recent* run id, and a diagnosis emitted
after the candidate suite would win that race.

Usage
-----
    python -m preflight.diagnose --baseline <sha> --candidate <sha>
    python -m preflight.diagnose --report /tmp/report.json     # a saved DiffReport
    python -m preflight.diagnose --baseline <a> --candidate <b> --dry-run

`preflight explain` was the name BUILD_PLAN gave this; wiring it into
`preflight/cli.py` is a one-line `add_command` follow-up that M6 did not make,
because `cli.py` was frozen while this was being built.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import sys
import time
import uuid
from typing import Any, Iterator

from opentelemetry.trace import SpanKind, format_trace_id

from preflight import budget, config, contracts, instrument, mcp, otel, replay
from preflight.contracts import DiffReport

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 2048
MAX_TURNS = 6
SERVICE_NAME = "preflight-diagnose"

# Cassettes for the diagnosis agent live apart from the golden suite's. The
# suite's `.cassettes/` are a committed fixture that CI replays; mixing an
# exploratory investigation into that directory would make the gate's inputs
# depend on how often somebody ran `explain`.
CASSETTE_DIR = os.getenv(
    "PREFLIGHT_DIAGNOSE_CASSETTES",
    str(replay.CASSETTE_DIR.parent / ".cassettes-diagnose"),
)

# The subset of the server's 42 tools the agent may call, and the parameters it
# may set on each. `start` / `end` / `searchContext` are injected rather than
# exposed -- the window is the gate's window, not the model's to choose, and a
# hallucinated one is a silently empty result rather than an error.
TOOL_SPEC: dict[str, tuple[str, ...]] = {
    "signoz_aggregate_traces": (
        "aggregation",
        "aggregateOn",
        "filter",
        "groupBy",
        "orderBy",
        "limit",
    ),
    "signoz_get_trace_details": ("traceId",),
}

TOOL_DESCRIPTIONS = {
    "signoz_aggregate_traces": (
        "Aggregate golden-suite spans in SigNoz. count/sum/avg/min/max/p95 over a "
        "numeric span attribute (aggregateOn, e.g. gen_ai.usage.input_tokens), "
        "filtered by a filter expression and grouped by attributes such as "
        "eval.case_id or gen_ai.tool.name. This is your main instrument."
    ),
    "signoz_get_trace_details": (
        "Every span of one trace, in order, with names, parents and durations. "
        "Use it to read the agent's trajectory for a single case."
    ),
}

SYSTEM = """\
You are Preflight's diagnosis agent. A CI gate has just failed a pull request \
because the AI agent under test got measurably worse, and a reviewer needs to \
know why in plain English.

Your only source of facts is the SigNoz MCP server, through the two tools you \
have been given. You cannot read the repository. Do not guess at file contents; \
reason from the telemetry.

The golden suite emits ONE TRACE PER CASE. Span attributes you can filter, \
group and aggregate on:

  eval.run_id, eval.case_id            which run and which case
  preflight.span_role                  case | llm | tool | retrieval
  preflight.cost_usd                   USD, on each llm span
  gen_ai.usage.input_tokens            on each llm span
  gen_ai.usage.output_tokens           on each llm span
  gen_ai.request.model                 on each llm span
  gen_ai.tool.name                     on each tool span
  preflight.retrieval.source           on each retrieval span

CRITICAL FILTER RULE: in a `filter` expression an unqualified key resolves to \
the RESOURCE attribute, not the span attribute, and silently matches nothing. \
Always write `attribute.eval.run_id = '...'`, `attribute.eval.case_id = '...'`, \
`attribute.preflight.span_role = 'llm'`. In `groupBy` and `aggregateOn` the \
bare name is correct -- qualify only inside `filter`.

The time window is injected for you. Never pass start, end or timeRange.

Work efficiently: you have at most {max_turns} turns and each tool result is \
truncated, so ask precise questions rather than dumping data. A good \
investigation is roughly: aggregate the metric that breached by case across \
both runs to find the worst case, then look at what changed structurally in \
that case -- span roles, tool names, retrieval sources, token split.

When you are done, reply with NO tool call and give the final diagnosis as 3-6 \
plain sentences that:
  1. name the single worst case and the metric(s) that regressed, with numbers;
  2. say what changed in the agent's behaviour (extra hops, extra tool calls, \
larger prompts, a different model) and cite the span evidence;
  3. state the most likely root cause in the pull request, and say plainly if \
the telemetry cannot distinguish between two causes.
Do not hedge with "may" or "might" where the spans are unambiguous. Do not \
recommend fixes; a reviewer wants the finding.
"""
# The wording above is load-bearing and was A/B'd against a tighter variant that
# demanded both-sides measurement and a concrete named root cause. See
# .plans/learnings.md 2026-07-27: the tighter prompt reasoned *better* about the
# cause (it correctly identified a system-prompt edit and ruled out a model
# swap) and investigated *worse* -- it never grouped by gen_ai.tool.name, so it
# asserted "the same tools appear in both runs", which is false. Steering the
# conclusion pulled effort away from gathering the evidence for it. This version
# finds `policy_search` and hedges the cause; that is the better failure.


class DiagnoseError(RuntimeError):
    pass


def _explain_api_failure(exc: Exception) -> DiagnoseError:
    """Turn a provider failure into a sentence that says what to do about it.

    Worth the twenty lines: the three ways this call fails on a project with a
    $1 ceiling and a shared key are authentication, exhausted credit, and a
    cassette miss, and an unadorned SDK traceback distinguishes none of them.
    "API key is invalid" in particular reads as a bug in this module until you
    notice it is a 401 from api.anthropic.com.
    """
    name = type(exc).__name__
    if isinstance(exc, replay.ReplayMiss):
        return DiagnoseError(
            f"{exc}\n\nThe diagnosis agent is running replay-only (no "
            "ANTHROPIC_API_KEY, or PREFLIGHT_REPLAY=1) and this turn is not "
            f"recorded in {CASSETTE_DIR}. Investigations are not deterministic "
            "-- SigNoz answers differ run to run -- so a cassette only hits if "
            "the whole transcript matches. Set a working key to record one."
        )
    if isinstance(exc, budget.BudgetExceeded):
        return DiagnoseError(f"{exc}")
    if "authentication" in name.lower() or "401" in str(exc):
        return DiagnoseError(
            f"the Anthropic API rejected the credentials ({exc}).\n"
            "ANTHROPIC_API_KEY in .env is missing, revoked or rotated. Check it "
            "against the free token-counting endpoint before re-running:\n"
            "  curl -s https://api.anthropic.com/v1/messages/count_tokens \\\n"
            "    -H \"x-api-key: $ANTHROPIC_API_KEY\" "
            "-H 'anthropic-version: 2023-06-01' \\\n"
            "    -H 'content-type: application/json' -d '{\"model\":"
            f"\"{MODEL}\",\"messages\":[{{\"role\":\"user\",\"content\":\"hi\"}}]}}'"
        )
    return DiagnoseError(f"the diagnosis call failed ({name}): {exc}")


# --- the report -> prompt facts -------------------------------------------


def _fmt(value: float, unit: str) -> str:
    if unit == "usd":
        return f"${value:.5f}"
    if unit == "ms":
        return f"{value:.0f}ms"
    if unit == "ratio":
        return f"{value:.3f}"
    return f"{value:.4g}"


def facts(report: DiffReport) -> str:
    """The gate's verdict, rendered for the model.

    Deliberately the *verdict* only -- headline deltas and the per-case table.
    Not an interpretation, and not the trace contents: if this block already
    explained the regression the agent would have nothing to investigate and
    the explanation would be a paraphrase of its own prompt.
    """
    lines = [
        "GATE VERDICT: " + ("FAILED" if report.breached else "passed"),
        f"baseline   {report.baseline_sha[:12]}  run={report.baseline_run_id}",
        f"candidate  {report.candidate_sha[:12]}  run={report.candidate_run_id}",
        "",
        "gated metrics (mean per task across the suite):",
        f"  {'metric':<26} {'baseline':>12} {'candidate':>12} {'change':>10}  breached",
    ]
    for d in report.deltas:
        pct = "inf" if d.pct_change == float("inf") else f"{d.pct_change:+.1f}%"
        lines.append(
            f"  {d.key:<26} {_fmt(d.baseline, d.unit):>12} "
            f"{_fmt(d.candidate, d.unit):>12} {pct:>10}  "
            + ("YES" if d.breached else "no")
        )

    for label, run in (("baseline", report.baseline), ("candidate", report.candidate)):
        if run is None:
            continue
        lines += [
            "",
            f"{label} run {run.run_id} -- per case:",
            f"  {'case':<16} {'cost_usd':>9} {'tokens':>8} {'tools':>6} "
            f"{'hops':>5} {'ms':>8}  trace_id",
        ]
        for c in sorted(run.cases, key=lambda c: -c.cost_usd):
            lines.append(
                f"  {c.case_id:<16} {c.cost_usd:>9.5f} {c.total_tokens:>8} "
                f"{c.tool_calls:>6} {c.retrieval_hops:>5} {c.duration_ms:>8.0f}  "
                f"{c.trace_id or '-'}"
            )
    if report.notes:
        lines += ["", "differ notes:"] + [f"  - {n}" for n in report.notes]
    return "\n".join(lines)


# --- the agent loop --------------------------------------------------------


@contextlib.contextmanager
def _cassettes(directory: str) -> Iterator[None]:
    """Point `replay` at the diagnosis cassette directory for the duration.

    `replay.CASSETTE_DIR` is a module global read at import time. Rebinding it
    here rather than editing `replay.py` keeps the golden suite's recorded
    fixtures -- which CI depends on -- out of reach of this agent.
    """
    from pathlib import Path

    previous = replay.CASSETTE_DIR
    replay.CASSETTE_DIR = Path(directory)
    try:
        yield
    finally:
        replay.CASSETTE_DIR = previous


WINDOW_BUCKET_MS = 10 * 60_000


def _window(lookback_minutes: int, end_ms: int | None = None) -> tuple[int, int]:
    """The [start, end] the MCP tools are pinned to, in epoch ms.

    The window is an *input to the cassette key*, because it appears in every
    tool call's arguments and therefore in the transcript. Two consequences:

    `end` is rounded up to the next 10 minutes, so two runs a minute apart ask
    SigNoz identical questions and the second replays for free. An un-rounded
    `now` would miss every cassette by a few milliseconds.

    ...and rounding alone only buys ten minutes, which is why `end_ms` can be
    pinned outright (`--window-end`, or `PREFLIGHT_DIAGNOSE_WINDOW_END`). A
    recorded investigation replays *exactly* only if it is asked the same
    question about the same slice of time, so the committed cassettes name the
    window they were recorded against. That is what makes `make m6-check`
    reproducible at $0 by someone who has no API key at all.
    """
    if end_ms is None:
        env = os.getenv("PREFLIGHT_DIAGNOSE_WINDOW_END")
        end_ms = int(env) if env else None
    if end_ms is None:
        end_ms = (
            (int(time.time() * 1000) // WINDOW_BUCKET_MS) + 1
        ) * WINDOW_BUCKET_MS
    return end_ms - lookback_minutes * 60_000, end_ms


@dataclasses.dataclass
class Diagnosis:
    text: str
    trace_id: str
    turns: int
    tool_calls: list[str]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    replayed: bool
    run_id: str

    def summary(self) -> str:
        # `cost_usd` is derived from the token counts, so on a replay it is the
        # cost this investigation *had* when it was recorded, not money spent
        # now. Saying which is not pedantry: the difference is the entire reason
        # a judge can re-run the check for free.
        mode = (
            "recorded cost; replayed from cassettes at $0"
            if self.replayed
            else "live API"
        )
        return (
            f"{self.turns} turn(s), {len(self.tool_calls)} MCP call(s), "
            f"{self.input_tokens}+{self.output_tokens} tokens, "
            f"${self.cost_usd:.5f} ({mode})"
        )


def diagnose(
    report: DiffReport,
    cfg: config.Config | None = None,
    *,
    lookback_minutes: int = 24 * 60,
    max_turns: int = MAX_TURNS,
    model: str = MODEL,
    window_end_ms: int | None = None,
) -> Diagnosis:
    """Investigate a failed gate over MCP and return a plain-English finding."""
    cfg = cfg or config.load()
    # Own service name, own SHA namespace: see the module docstring. This must
    # not look like a suite run to `differ.resolve_run`.
    cfg = dataclasses.replace(cfg, service_name=SERVICE_NAME)
    sha_tag = f"diagnosis-{report.candidate_sha[:12]}"
    otel.setup(cfg, commit_sha=sha_tag)

    run_id = f"diag-{uuid.uuid4().hex[:12]}"
    ctx = instrument.RunContext(run_id=run_id, commit_sha=sha_tag, case_id="diagnosis")
    start_ms, end_ms = _window(lookback_minutes, window_end_ms)

    search_context = (
        "Preflight CI gate failed on candidate "
        f"{report.candidate_sha[:12]} vs baseline {report.baseline_sha[:12]}; "
        "diagnose which golden-suite case and which metric regressed and why."
    )

    system = SYSTEM.format(max_turns=max_turns)
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                facts(report)
                + "\n\nInvestigate this regression in SigNoz and give the diagnosis."
            ),
        }
    ]

    tool_calls: list[str] = []
    in_tokens = out_tokens = 0
    cost = 0.0
    all_replayed = True
    final = ""
    turns = 0

    with mcp.MCPClient(client_name="preflight-diagnose") as client:
        tools = mcp.anthropic_tools(
            client, TOOL_SPEC, overrides=TOOL_DESCRIPTIONS
        )
        with otel.tracer().start_as_current_span(
            "diagnose regression",
            kind=SpanKind.INTERNAL,
            attributes={
                **ctx.attributes(),
                instrument.PREFLIGHT_SPAN_ROLE: "diagnosis",
                "preflight.diagnosis.baseline_sha": report.baseline_sha,
                "preflight.diagnosis.candidate_sha": report.candidate_sha,
                "preflight.diagnosis.baseline_run_id": report.baseline_run_id,
                "preflight.diagnosis.candidate_run_id": report.candidate_run_id,
                "preflight.diagnosis.breached": ",".join(
                    d.key for d in report.deltas if d.breached
                ),
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.request.model": model,
            },
        ) as root:
            trace_id = format_trace_id(root.get_span_context().trace_id)

            with _cassettes(CASSETTE_DIR):
                for turn in range(max_turns):
                    turns = turn + 1
                    with instrument.llm_span(ctx, cfg, model=model) as llm:
                        try:
                            response, replayed = replay.complete(
                                cfg,
                                model=model,
                                system=system,
                                messages=messages,
                                tools=tools,
                                max_tokens=MAX_TOKENS,
                            )
                        except Exception as exc:  # noqa: BLE001 - re-raised below
                            raise _explain_api_failure(exc) from exc
                        usage = response["usage"]
                        llm.input_tokens = usage["input_tokens"]
                        llm.output_tokens = usage["output_tokens"]
                        llm.response_model = response.get("model") or model

                    all_replayed = all_replayed and replayed
                    in_tokens += usage["input_tokens"]
                    out_tokens += usage["output_tokens"]
                    cost += cfg.price(model).cost_usd(
                        usage["input_tokens"], usage["output_tokens"]
                    )

                    blocks = response["content"]
                    text = "\n".join(
                        b.get("text", "") for b in blocks if b.get("type") == "text"
                    ).strip()
                    requests = [b for b in blocks if b.get("type") == "tool_use"]

                    if not requests:
                        final = text
                        break

                    if text:
                        final = text  # keep the last prose in case turns run out
                    messages.append({"role": "assistant", "content": blocks})

                    results = []
                    for call in requests:
                        name = call["name"]
                        args = {
                            **call.get("input", {}),
                            "start": start_ms,
                            "end": end_ms,
                            "searchContext": search_context,
                        }
                        tool_calls.append(name)
                        with instrument.tool_span(
                            ctx, name=name, call_id=call["id"]
                        ) as span:
                            span.set_attribute(
                                "preflight.mcp.arguments",
                                json.dumps(call.get("input", {}), default=str)[:1800],
                            )
                            span.set_attribute("preflight.mcp.server", client.url)
                            try:
                                payload = client.call(name, args)
                                body = mcp.render_result(payload)
                                is_error = False
                            except mcp.MCPError as exc:
                                body = f"MCP error: {exc}"[:1200]
                                is_error = True
                            span.set_attribute("preflight.mcp.result_chars", len(body))
                            span.set_attribute("preflight.mcp.is_error", is_error)
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": call["id"],
                                "content": body,
                                **({"is_error": True} if is_error else {}),
                            }
                        )
                    # Spend the last turn concluding, not investigating. Without
                    # this the agent can burn turn N on a tool call and never
                    # get to write the diagnosis, and the run ends holding data
                    # nobody asked for instead of an answer. Tool results must
                    # lead the user turn; trailing text is allowed.
                    if turn == max_turns - 2:
                        results.append(
                            {
                                "type": "text",
                                "text": "You have ONE turn left. Make no further "
                                "tool calls: write the final diagnosis now from "
                                "what you already have.",
                            }
                        )
                    messages.append({"role": "user", "content": results})
                else:
                    final = (
                        final
                        or "The diagnosis agent ran out of turns before concluding."
                    )

            root.set_attribute("preflight.diagnosis.turns", turns)
            root.set_attribute("preflight.diagnosis.mcp_calls", len(tool_calls))
            root.set_attribute("preflight.diagnosis.text", final[:4000])
            root.set_attribute(instrument.PREFLIGHT_COST_USD, cost)

    otel.force_flush()
    return Diagnosis(
        text=final.strip(),
        trace_id=trace_id,
        turns=turns,
        tool_calls=tool_calls,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        cost_usd=cost,
        replayed=all_replayed,
        run_id=run_id,
    )


# --- entrypoint ------------------------------------------------------------


def _report_from_json(path: str) -> DiffReport:
    """Rebuild a `DiffReport` from `differ.to_dict` output.

    Lets the GitHub Action diagnose the same report it commented with, rather
    than re-querying and risking a different answer.
    """
    raw = json.loads(open(path).read())

    def run(d: dict[str, Any] | None) -> contracts.RunSummary | None:
        if not d:
            return None
        return contracts.RunSummary(
            run_id=d["run_id"],
            commit_sha=d["commit_sha"],
            cases=[
                contracts.CaseSummary(
                    case_id=c["case_id"],
                    spans=c.get("spans", 0),
                    input_tokens=c.get("input_tokens", 0),
                    output_tokens=c.get("output_tokens", 0),
                    cost_usd=c.get("cost_usd", 0.0),
                    duration_ms=c.get("duration_ms", 0.0),
                    tool_calls=c.get("tool_calls", 0),
                    retrieval_hops=c.get("retrieval_hops", 0),
                    success=c.get("success", True),
                    trace_id=c.get("trace_id", ""),
                )
                for c in d.get("cases", [])
            ],
        )

    return DiffReport(
        baseline_sha=raw["baseline_sha"],
        candidate_sha=raw["candidate_sha"],
        baseline_run_id=raw["baseline_run_id"],
        candidate_run_id=raw["candidate_run_id"],
        deltas=[
            contracts.MetricDelta(
                key=d["key"],
                baseline=d["baseline"],
                candidate=d["candidate"],
                threshold=d["threshold"],
                breached=d["breached"],
                unit=d.get("unit", "count"),
            )
            for d in raw.get("deltas", [])
        ],
        baseline=run(raw.get("baseline")),
        candidate=run(raw.get("candidate")),
        notes=list(raw.get("notes", [])),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Explain a failed Preflight gate.")
    ap.add_argument("--baseline", help="baseline commit SHA")
    ap.add_argument("--candidate", help="candidate commit SHA")
    ap.add_argument("--report", help="path to a JSON DiffReport (differ.to_dict)")
    ap.add_argument("--lookback-minutes", type=int, default=24 * 60)
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS)
    ap.add_argument(
        "--window-end",
        type=int,
        default=None,
        help="pin the query window's end (epoch ms) so a recorded "
        "investigation replays byte-for-byte; see _window()",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the prompt and the MCP tool definitions; call no model",
    )
    ap.add_argument("--json", action="store_true", help="emit the diagnosis as JSON")
    ap.add_argument(
        "--print-trace-id",
        action="store_true",
        help="print only the trace id of the diagnosis agent's own run",
    )
    args = ap.parse_args(argv)

    cfg = config.load()
    if args.report:
        report = _report_from_json(args.report)
    elif args.baseline and args.candidate:
        from preflight.differ import DiffError, diff

        try:
            report = diff(
                args.baseline,
                args.candidate,
                cfg,
                lookback_minutes=args.lookback_minutes,
            )
        except DiffError as exc:
            print(f"cannot diff: {exc}", file=sys.stderr)
            return 2
    else:
        ap.error("give --report, or both --baseline and --candidate")

    if args.dry_run:
        with mcp.MCPClient(client_name="preflight-diagnose") as client:
            tools = mcp.anthropic_tools(client, TOOL_SPEC, overrides=TOOL_DESCRIPTIONS)
        print(SYSTEM.format(max_turns=args.max_turns))
        print("--- tools ---")
        print(json.dumps(tools, indent=1))
        print("--- facts ---")
        print(facts(report))
        chars = len(SYSTEM) + len(json.dumps(tools)) + len(facts(report))
        print(f"\n[dry run] ~{chars // 4} input tokens on turn 1; no model called.")
        return 0

    spend = budget.read()
    print(
        f"budget: ${spend.total_usd:.4f} of ${spend.cap_usd:.2f} spent "
        f"across {spend.calls} calls",
        file=sys.stderr,
    )

    result = diagnose(
        report,
        cfg,
        lookback_minutes=args.lookback_minutes,
        max_turns=args.max_turns,
        window_end_ms=args.window_end,
    )

    if args.print_trace_id:
        print(result.trace_id)
        return 0
    if args.json:
        print(json.dumps(dataclasses.asdict(result), indent=2))
        return 0

    print("\n=== Preflight diagnosis " + "=" * 47)
    print(result.text)
    print("=" * 70)
    print(f"investigation trace : {result.trace_id}")
    print(f"diagnosis run id    : {result.run_id}")
    print(f"mcp calls           : {', '.join(result.tool_calls) or 'none'}")
    print(f"cost                : {result.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
