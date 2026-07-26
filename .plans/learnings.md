# Learnings

Newest first.

---

## 2026-07-27 — M2 golden suite: real agent, cassette replay, metrics, logs

**What changed.** `agent/reference.py` is now a real tool-calling loop against
`claude-haiku-4-5-20251001` over an in-repo fake dataset (`agent/data.py`):
one grounding retrieval hop, then up to five model turns with three tools
(`lookup_order` / `policy_search` / `check_inventory`). Six cases in
`suite/cases/`, 5–13 spans each. Spans carry the full GenAI semconv set plus
`preflight.cost_usd` from the price table; run-level metrics are emitted on
`eval.run_id` / `eval.case_id` / `vcs.commit_sha` under the names pinned in
`contracts.py`; logs export over OTLP with trace context attached.
`query.run_summary_typed()` returns `contracts.RunSummary` — the M3 seam.
Branch `seeded-regression` holds a one-line prompt edit worth +150% cost.

**The whole thing runs on a record/replay cache.** `preflight/replay.py` keys
each request by a hash of (model, system, messages, tools, max_tokens,
temperature) and stores the response as JSON under `.cassettes/`, which is
committed on purpose. `PREFLIGHT_REPLAY=1` — or simply no API key — replays and
never calls out, so a fresh clone runs the full suite offline and for free. Two
problems, one fix: the project has $1 of credit, and a gate whose numbers move
between runs is not a gate.

**Takeaway: a test double has to be faithful in whatever dimension you gate on,
not just the obvious one.** The first cassette design replayed tokens and
content perfectly and dropped latency on the floor — replayed cases finished in
~0.2ms. Tokens and cost were exact, so the cache looked correct, and
`p95_latency_ms` had quietly become a random number generator: ordinary
0.2ms/0.5ms jitter is a +150% swing that trips any threshold anyone would pick.
That is the "gate is flaky, so the team switches it off" failure in the risk
register, arriving through the component built to make the gate *stable*. Fix
was to record the provider's wall time in the cassette and sleep it on replay;
p95 is now 3.19s baseline vs 8.54s regression — a real, reproducible signal.
M3 independently hit the same zero-latency problem from the other side and added
an absolute noise floor; both fixes are worth keeping.

**Gotcha: the harness ceiling silently caps the signal it is supposed to
measure.** With `MAX_MODEL_TURNS = 3`, the seeded regression came out at +16%
tokens — precisely the "15%, not 3×" case BUILD_PLAN warns against — and the
obvious read was "the prompt edit is too weak." It wasn't: the *cap* was
truncating the trajectory, so a longer-running agent was being clipped back into
looking like baseline. Raising the ceiling to 5 changed nothing for baseline
(every case still terminates on `end_turn` in two calls, all twelve cassettes
still hit) and let the regression show +130%. Check the instrument before
concluding the effect is small.

**Gotcha, and the most portable finding here: what inflates agent cost is
serialisation, not verbosity.** Two attempts at the seeded regression asked the
model to be more thorough — "enumerate every option", "check every relevant
tool, one per step". Both landed at +16–18% tokens, because the model batches
its tool calls into a single parallel turn: more tools, same number of turns,
input context re-sent the same number of times. What actually moved the number
was a **dependency chain** — call `policy_search`, then `lookup_order`, then
`check_inventory` *for the SKU you found on that order* — where each tool's
input requires the previous tool's output. That forces genuinely sequential
turns, and each turn re-sends a growing transcript: +130% tokens, +133%
retrieval hops, +150% cost. Worth remembering when reasoning about agent spend:
turn count is the multiplier, output length is rounding error.

**Gotcha: SigNoz explodes OTel histograms into suffixed series, and only
`.bucket` is registered as queryable.** The six `preflight.case.*` histograms
land in ClickHouse as `.bucket` / `.count` / `.sum` / `.min` / `.max`, with
correct `eval.run_id` / `eval.case_id` / `vcs.commit_sha` labels — but
`signoz_metrics.distributed_metadata` records only `.bucket` with
`type = Histogram`. Every `signal: "metrics"` query I tried through
`/api/v5/query_range` returned zero rows (base name, `.sum`, and `.bucket`;
with and without `temporality: Cumulative`; several `spaceAggregation` values).
The data is unambiguously there — verified in ClickHouse — so this is a read-path
shape I did not crack, not a missing emit. Left for M5, which BUILD_PLAN already
says to build through the SigNoz MCP server rather than by hand-rolling these
payloads. **Nothing depends on it today:** the gate reads traces via
`run_summary_typed`, which is fully verified end to end.

**`temperature=0` on a golden suite.** Not for determinism at inference — it
does not give that — but so that *re-recording* cassettes doesn't rewrite every
expected answer and force the six `expect_contains` assertions to be re-tuned.

**Process, learned the annoying way: `git add -A` in a shared working tree
commits your teammates' work in progress.** Three agents share this checkout;
my first M2 commit swept in M3's then-unfinished `differ.py`, `cli.py`, and
`scripts/m3_check.py`. Nothing was lost and their files imported cleanly, so
unwinding it would have risked more than leaving it — but the fix going forward
is to stage explicit paths. Same lesson for branches: the `seeded-regression`
work was done in a `git worktree` at /tmp rather than by checking the branch out
here, which would have yanked the tree out from under two other agents mid-edit.

**Spend: $0.1123 across 69 calls**, against a $1.00 cap and a $0.30 target —
`preflight/budget.py` gated every one. Recording the six-case suite once costs
$0.0151; the rest went on one probe, one re-record after adding latency and
`temperature`, and three attempts at the seeded regression. Every subsequent run
of the suite is free.

---

## 2026-07-27 — M4 landed: the PR comment, and the deep link that isn't a guess

**What changed.** `preflight/report.py` is the real renderer: pass/fail banner,
a headline naming the breached metrics worst-first, the delta table, a "biggest
mover" line pointing straight at the offending trace, and a collapsed per-case
breakdown where every row deep-links to its own trace in SigNoz.
`.github/workflows/preflight.yml` forges SigNoz in-job from the committed
`casting.yaml`, bootstraps credentials, resolves the baseline from
`git merge-base`, runs the suite on the merge base *in a detached worktree* and
on the PR head, gates, and upserts one sticky PR comment. `PREFLIGHT_REPLAY=1`
throughout, so CI costs nothing and is deterministic.
`scripts/report_sample.py` renders samples and verifies links; `make ci-local`
rehearses the whole Action on a laptop.

**The deep-link format, and how it was actually found.** The plan said to copy
it out of the address bar. The bundle is a better source, because it explains
*why* the URL is the shape it is — and the M1 precedent (stale auth docs
recovered from `/assets/index-*.js`) said to look there first. Four independent
confirmations, all against v0.134.0:

1. `ROUTES.TRACE_DETAIL: '/trace/:id'` — a single trace, not `/traces/`.
2. The trace-detail chunk (`TraceDetailsV3-*.js`) reads the selected span from
   the query string: `searchParams.get('spanId')`, and a present `spanId` means
   "expand the waterfall to this span".
3. The UI's own **Copy link** handler builds `` `${pathname}?${params}` `` with
   `spanId` set — so the template is literally what SigNoz puts on your
   clipboard.
4. `POST /api/v4/traces/{id}/waterfall` returns the span list for a real trace
   and `{"type":"not-found"}` for a bogus one.

        {base}/trace/{trace_id}                    # the trace
        {base}/trace/{trace_id}?spanId={span_id}   # that span, pre-selected

**Verifying a link means asking the API, not asking for a 200.** SigNoz is a
SPA behind a catch-all: *every* path returns HTTP 200 with the same HTML shell,
including `/trace/deadbeef` and `/trace/this-is-not-a-trace`. A status-code
check would have "verified" a format that was completely wrong — the same trap
that made the stale auth endpoints in M1 look like they worked. The real check
is the API the page calls to populate itself, which is why
`scripts/report_sample.py --verify` hits `/api/v4/traces/{id}/waterfall` and
asserts a non-empty span list. All 6 links in a real gate report pass it.

**A gate that can't run must not look like a gate that failed.** `preflight
run` exits 2 when ingest never settles and `diff` exits 3 when there's nothing
to compare; neither means the agent regressed. Both get their own annotation
and a comment that says *the comparison did not happen*. Every path that
produces no `report.md` synthesises one — silence in CI is exactly how a broken
gate goes unnoticed, which is the failure mode this project exists to prevent.

**Gotcha: GitHub's default shell is `bash -eo pipefail`, so `code=${PIPESTATUS[0]}`
never runs.** The step dies on the failing command before it can capture the
exit code it exists to capture — and the gate's whole design is "post the
comment, *then* fail". Every step that inspects an exit code says `set +e`
first. Caught by extracting each `run:` block out of the YAML and executing it
under `bash --noprofile --norc -eo pipefail`, which is worth doing for any
workflow you can't afford to debug by pushing commits.

**Takeaway.** When the docs and the address bar are both weak sources, the
frontend bundle is the strongest one available — it gives you the format *and*
the reason. And when the server answers 200 to everything, "it returned 200" is
not verification; find the request the page makes to render itself and check
that instead.

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
