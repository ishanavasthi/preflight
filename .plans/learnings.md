# Learnings

Newest first.

---

## 2026-07-27 — M3 differ + gate landed; the gate fires and stays quiet

**What changed.** `preflight/differ.py` resolves a commit SHA to its most recent
`eval.run_id` in SigNoz, pulls both runs, computes all six metrics in
`contracts.GATED_METRICS` through `RunSummary.metric()`, applies the thresholds
in `preflight.yaml`, and returns a `DiffReport`. `preflight diff --baseline
<sha> --candidate <sha>` renders it through M4's `report.render_markdown`,
supports `--format markdown|json` and `--output <path>`, and exits 1 on breach.
Thresholds for all six metrics are in `preflight.yaml` with their reasoning.
`scripts/m3_check.py` is the acceptance check and calls no model.

**Measure the variance, don't guess it.** The plan says to set thresholds "well
above observed run-to-run variance", so the first move was to run the real suite
twice under two SHAs and diff it: 0.0% on cost, tokens, tool calls and hops
(cassette replay makes those exact), 0.2% on latency. Thresholds sit at 25–75%,
which is 100×+ the measured noise — and they are sized for re-recorded
cassettes, not for this 0%.

**Gotcha, and it is a nasty one: an unqualified attribute name in a SigNoz
filter expression resolves to the _resource_ attribute, not the span
attribute.** `vcs.commit_sha = 'abc'` matched 0 spans while
`attribute.vcs.commit_sha = 'abc'` matched 42, because `otel.py` stamps the
resource once per process. In ordinary CI — one process, one commit — the two
values agree and the bug is invisible. `groupBy` is immune because it takes an
explicit `fieldContext`, which is exactly why the M1 probe looked fine. Qualify
every filter.

**Gotcha: percentages are meaningless near zero, and a gate that cries wolf gets
switched off.** The first end-to-end run reported a +205% p95 latency regression
between two *identical* runs, because the synthetic cases took 0.12ms. Fixed
with an absolute noise floor (`p95_latency_ms_abs_floor_ms: 25`): a rise must
clear 25ms as well as the percentage. Latency is the only metric with a
near-zero regime, so it is the only one with a floor.

**Gotcha: `max(timestamp)` works and returns epoch seconds**, so SHA → newest
run is one scalar query rather than a bisection over time windows. But string
intrinsics can't be aggregated at all — `any()` is not a recognised function and
`max(trace_id)` makes ClickHouse try to cast hex to Float64. Trace ids need a
`fieldContext: "span"` group-by instead.

**Gotcha: a reader's default lookback silently truncates a valid diff.**
`query.run_summary_typed` defaults to 60 minutes; `resolve_run` will happily
find a day-old baseline. Not threading the window through turns "your baseline
is from yesterday" into "baseline run has no cases".

**Takeaway.** Every real bug in this milestone was a *silent* one — a filter that
matched the wrong field, a percentage of a near-zero number, a window that
quietly excluded the data. None of them threw. The offline fixture tests caught
none of them and the single end-to-end run against real SigNoz caught all three,
which is the argument for running the acceptance check the moment the milestone
ends rather than batching it to the morning.

---

## 2026-07-27 — M1 walking skeleton landed; both design-changing unknowns resolved

**What changed.** Brought up SigNoz v0.134.0 via Foundry on OrbStack with the MCP
server enabled, built the telemetry and query layers, and got the M1 acceptance
check passing: `preflight run --cases 1` followed by `preflight query --run-id`
prints a non-zero span count sourced from the HTTP API for a run created 5
seconds earlier.

**Unknown #1 — can SigNoz group by a custom span attribute? Yes.** This was the
load-bearing question; if it had failed, the SigNoz-as-source-of-truth design
would have died. `groupBy: [{name: "eval.run_id", fieldContext: "attribute"}]`
returns one row per run, and multi-aggregation works in a single round trip — so
the one-query-per-run fallback held in reserve isn't needed.

**Unknown #2 — ingest lag is 2–4s**, matching the "seconds, not minutes"
assumption. Timeout stays at 120s regardless.

**Gotcha: the SigNoz auth API in the docs is stale, and it fails *silently*.**
`/api/v1/login` and `/api/v1/pats` no longer exist in v0.134.0 — they fall
through to the SPA catch-all, which returns **HTTP 200 with an HTML body**. A
client that checks the status code sees success and then dies on JSON parse. The
real surface is `POST /api/v2/sessions/email_password` (requires `orgID`) and
`/api/v1/service_accounts/{id}/keys`. Recovered it by grepping the frontend JS
bundle for API paths, which was far faster than guessing.

**Gotcha: API keys are now service-account keys and start with zero permissions.**
A fresh service account authenticates fine and then returns `authz_forbidden` on
every call until a role is attached via `POST .../roles` with `{"id": "<roleId>"}`
— note the field is `id`, not `roleId`.

**Gotcha: `query_range` does not echo aggregation aliases.** Columns come back as
`__result_0`, `__result_1`, … in request order, with group-by columns named after
the attribute and marked `columnType: "group"`. The response nests rows at
`data.data.results[].data` as positional lists, not under an `aggregations[]` key
as assumed. `_flatten_scalar` re-attaches aliases by `aggregationIndex`.

**Takeaway: the ingest poller paid for itself before it ever ran in CI.** It was
built to guard against a real risk (diffing a half-ingested run), but its first
act was to catch my own response-parser bug — it reported `0/6` spans instead of
letting a broken flattener return an empty list that downstream code would have
read as "no regression." A component that fails loudly on the *expected* failure
mode also surfaces the unexpected ones. Worth remembering when tempted to defer
this kind of guard to "later."

**Second takeaway: when vendor docs and a running instance disagree, read the
client.** The frontend bundle is a complete, current, executable description of
the API surface. Grepping it resolved in minutes what endpoint-guessing was not
converging on.

**Environment notes.** No container runtime existed on the machine; installed
OrbStack. It only symlinks `docker` into `PATH` after GUI first-run, but ships
the binaries at `/Applications/OrbStack.app/Contents/MacOS/xbin/` immediately —
the Makefile prepends that unconditionally rather than depending on a click.
