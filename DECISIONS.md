# Decisions

Deviations from `BUILD_PLAN.md`, and the unknowns it flagged, resolved in the
order they were hit. Per the plan, changes here are logged, not silent.

---

## M1 — Walking skeleton

### Container runtime: OrbStack, not Docker Desktop

The build machine had no container runtime at all. Chose OrbStack for startup
speed and RAM footprint on a laptop running the full SigNoz stack.

One wrinkle worth knowing: OrbStack only symlinks `docker` into `PATH` after its
GUI first-run completes. The binaries live in
`/Applications/OrbStack.app/Contents/MacOS/xbin/` from the moment it installs,
so the `Makefile` prepends that directory unconditionally rather than depending
on the GUI having been clicked through.

### Unknown #1 — can SigNoz aggregate and group by a custom span attribute?

**Resolved: yes.** This was the load-bearing question; the SigNoz-as-source-of-
truth design survives, and the fallback in the kill criteria is not needed.

Verified against SigNoz **v0.134.0**, `POST /api/v5/query_range`:

```jsonc
{
  "schemaVersion": "v1",
  "start": 1785095000000, "end": 1785098600000,
  "requestType": "scalar",
  "compositeQuery": {"queries": [{
    "type": "builder_query",
    "spec": {
      "name": "A", "signal": "traces",
      "aggregations": [{"expression": "count()"}],
      "filter": {"expression": "eval.run_id EXISTS"},
      "groupBy": [{"name": "eval.run_id", "fieldContext": "attribute"}]
    }
  }]}
}
```

returns one row per run. Multi-aggregation works the same way, so per-case token
and cost totals come back in a single round trip — no need for the one-query-
per-run fallback the plan held in reserve.

**Response shape, which the plan did not have and which cost real time:** rows
are at `data.data.results[].data` as positional lists matching
`data.data.results[].columns[]`. Group-by columns carry `columnType: "group"`
and are named after the attribute. **Aggregation columns are named
`__result_0`, `__result_1`, … in request order — the `alias` you send is not
echoed back.** `preflight/query.py::_flatten_scalar` re-attaches aliases by
`aggregationIndex` so the rest of the codebase can use readable names.

### Unknown #2 — how long after a run can CI query the traces?

**Assumption held: seconds, not minutes.** Observed end-to-end lag from
`force_flush()` to spans being queryable is **~2–4s** on this deployment
(2 poll cycles at a 2s interval).

The poller ships anyway, as the plan required, and is not a formality: it caught
the flattener bug below by reporting `0/6` instead of quietly diffing nothing.
Timeout stays at 120s — far above the observed lag, per the "raise the timeout
rather than shorten it" note in the risks section.

### SigNoz auth API moved — the documented endpoints are stale

The plan and the current SigNoz docs describe `POST /api/v1/login` and PAT
creation at `/api/v1/pats`. **Neither exists in v0.134.0**; both fall through to
the SPA catch-all, which returns *HTTP 200 with an HTML body* — so a naive
client sees success and fails on parse. The working surface, recovered from the
frontend bundle:

| Purpose | Endpoint |
|---|---|
| First-user registration | `POST /api/v1/register` |
| Login | `POST /api/v2/sessions/email_password` (requires `orgID`) |
| List roles | `GET /api/v1/roles` |
| Create service account | `POST /api/v1/service_accounts` (`{"name": "..."}`, lowercase/hyphens only) |
| Grant a role | `POST /api/v1/service_accounts/{id}/roles` with `{"id": "<roleId>"}` |
| Mint API key | `POST /api/v1/service_accounts/{id}/keys` |

API keys are now **service-account keys**, not PATs, and a fresh service account
has **no permissions** until a role is attached — the key authenticates but every
call returns `authz_forbidden`. `scripts/bootstrap_signoz.sh` encodes the whole
sequence so a judge re-running Foundry gets a working `.env` in one command.

### Reference agent is a stub in M1

`agent/reference.py` emits real spans with deterministic pseudo-token counts
rather than calling a model. M1's job is to prove the telemetry round-trip, and
a deterministic agent makes the ingest poller testable without spend or noise.
The real `claude-sonnet-5` call lands in M2, which is where the plan puts it.

### Not yet done, deferred as planned

- Run-level **metrics** are wired (`MeterProvider` + OTLP exporter in
  `preflight/otel.py`) but nothing emits them yet — M2.
- **Logs with trace context** — M2.
- The SigNoz **trace deep-link format** is still unresolved. Per the plan it gets
  copied out of the UI address bar when `report.py` is written in M4, not guessed.

---

## M3 — Differ + gate

### Thresholds, and why they are where they are

The plan's non-determinism risk says to set thresholds "well above observed
run-to-run variance". So the first thing M3 did was measure the variance rather
than guess it: two runs of the identical suite, same code, both read back out of
SigNoz.

| Metric | Observed variance, run to run | Threshold | Margin |
|---|---|---|---|
| `cost_usd_per_task` | 0.0% | +25% | ∞ |
| `total_tokens_per_task` | 0.0% | +25% | ∞ |
| `p95_latency_ms` | 0.2% | +75% (and ≥25ms) | ~375× |
| `tool_calls_per_task` | 0.0% | +40% | ∞ |
| `retrieval_hops_per_task` | 0.0% | +50% | ∞ |
| `success_rate` | 0.0 | drop > 0.01 | — |

Cost, tokens and trajectory are *exactly* zero-variance because M2's cassette
replay makes the suite deterministic — the same request hash returns the same
response forever. That is the point of the cassettes, and it is what makes the
gate meaningful: a delta can only come from a prompt or code change, never from
sampling. The thresholds are therefore not tuned against this 0%; they are sized
for the day someone re-records the cassettes live, where the drift is real.

Latency is the exception and is deliberately the loosest number in the table: it
is wall-clock, so it moves with CI runner load and provider latency, neither of
which the PR under review changed. Latency here is a smoke signal; cost is the
gate.

`success_rate` is gated on an **absolute drop**, not a percentage — a percentage
of a ratio reads badly in a PR comment. 0.01 is not a tolerance for failure, it
is a tolerance for float noise: one newly failing case in a six-case suite is a
drop of 0.167, sixteen times the threshold.

### New: an absolute noise floor on latency

`p95_latency_ms_abs_floor_ms: 25` is not in BUILD_PLAN.md. It exists because the
first end-to-end run of the gate reported a **+205% latency regression between
two identical runs**: the cases took 0.12ms, and a percentage change is unbounded
as the denominator approaches zero. A rise must now clear 25ms in absolute terms
*as well as* the percentage before it gates, and a rise suppressed by the floor
is written into `DiffReport.notes` rather than silently dropped.

This is the "gate is flaky, CI fails for the wrong reason" risk from the plan's
risk register, caught in the milestone that introduced it. No other metric gets
a floor — cost and token counts have no near-zero regime where this happens.

### Gotcha: unqualified attribute names in a filter resolve to the *resource*

`filter.expression` of `vcs.commit_sha = 'abc'` does **not** match the span
attribute of that name. It matches the **resource** attribute, which
`preflight/otel.py` stamps once per process at `setup()`. The two agree in
normal CI — one process, one commit — so this is invisible until something
emits runs for two commits from one process, at which point the SHA lookup
returns zero rows for a run that plainly exists.

`groupBy` has no such problem: it takes an explicit `fieldContext: "attribute"`,
which is why the M1 probe worked. The differ now qualifies every filter
explicitly (`attribute.vcs.commit_sha`, `attribute.eval.run_id`,
`attribute.preflight.span_role`). Verified against SigNoz v0.134.0: the
unqualified form returned 0 spans where `attribute.`-qualified returned 42.

### SHA → run id resolution

`max(timestamp)` **is** a valid aggregation on traces and comes back as epoch
*seconds* (float). So resolving a commit to its most recent run is one scalar
query grouped by `eval.run_id`, not a per-run round trip or a time-window
bisection. A SHA with several runs (a re-run, a CI retry) resolves to the newest
and the report notes that siblings existed.

Trace ids cannot be retrieved this way — `any()` is not a recognised function
and `max(trace_id)` makes ClickHouse try to cast a hex string to Float64.
`query.run_summary_typed` gets them with an explicit `fieldContext: "span"`
group-by instead.

### Failure modes are reported, not swallowed

A `DiffError` is not a regression verdict, and the CLI keeps them apart:
**exit 1** means a gated metric breached, **exit 3** means the diff could not be
computed. Collapsing the two would make "the baseline run aged out of the
lookback window" render in CI as "this PR broke the agent", which is the kind of
false red that gets a gate switched off.

Handled explicitly: baseline SHA has no runs; candidate SHA has no runs; both
SHAs resolving to the same run; a zero baseline (gate on the absolute rise, note
that the percentage is undefined); and case sets differing between runs — which
compares only the intersection and names the excluded cases in the notes, rather
than dividing two different populations by two different denominators and
calling the result a delta.

### Deviation: the check was proven with synthetic runs, not the seeded branch

BUILD_PLAN's M3 check is "on the seeded regression branch → exit 1; on baseline
→ exit 0". At the time M3 finished, `seeded-regression` pointed at the same
commit as `main` — M2 still had the prompt edit in flight — so the branch half
of that check could not run.

What was proven instead, and it is not weaker on the gate itself:

- **Exit 0 on real suite data.** Two runs of the real agent under cassette
  replay, tagged with two different SHAs, both read back from SigNoz: every
  metric within threshold, exit 0.
- **Exit 1 with the cost delta named**, from three real runs emitted into
  SigNoz — a baseline, a noise-sized re-run, and one with 3× the tokens and an
  extra retrieval hop — diffed through the real `/api/v5/query_range`. Cost
  +200.0%, and every other gated metric fires too.

`scripts/m3_check.py` runs both layers and calls no model, so it costs nothing
against the $1 cap. Re-running the branch half once M2 lands the prompt edit is
a five-minute job and belongs to M4's end-to-end check anyway.

### Not done in M3

- **Trajectory divergence** in the plan's richer sense (edit distance over the
  tool-call *sequence*) is not implemented. `tool_calls_per_task` counts calls;
  it does not notice a reordered trajectory with the same count. The kill
  criterion allowed cutting this metric entirely — the count version was cheap
  and is in, the sequence version was not attempted. Say "tool-call volume", not
  "trajectory divergence", in the submission.

---

## M4 — GitHub Action + PR comment

### The trace deep-link format — resolved from the frontend bundle, not the address bar

The plan (and M1's "not yet done" list) left this open with an explicit
instruction: copy it out of the SigNoz UI address bar, do not guess it. It was
resolved a stronger way — read out of the shipped frontend bundle, the same
technique that recovered the moved auth endpoints in M1 — and then verified
against the API. Four independent confirmations, all against **v0.134.0**:

| # | Source | Finding |
|---|---|---|
| 1 | `/assets/index-*.js` router constants | `ROUTES.TRACE_DETAIL: '/trace/:id'` (and `TRACE_DETAIL_OLD: '/trace-old/:id'`, a pure redirect *to* `/trace/${id}` preserving `location.search`) |
| 2 | `TraceDetailsV3-*.js` | reads the selected span as `searchParams.get('spanId')`; a present `spanId` sets `isUncollapsed: true` |
| 3 | `TraceDetailsV3-*.js` *Copy link* handler | builds ``` `${pathname}?${params}` ``` with `spanId` set — i.e. the template below is what the UI itself puts on your clipboard |
| 4 | `POST /api/v4/traces/{id}/waterfall` | 200 + full span list for a real trace id; `{"type":"not-found"}` for a bogus one |

```
{base_url}/trace/{trace_id}                    # the trace
{base_url}/trace/{trace_id}?spanId={span_id}   # that span, pre-selected
```

**Why a status-code check was not enough.** SigNoz serves a SPA behind a
catch-all, so *every* path returns HTTP 200 with the same HTML shell — including
`/trace/not-a-real-trace`. This is the identical trap that made the stale
`/api/v1/login` endpoint look like it worked in M1. Verification therefore hits
the API the trace-detail page calls to populate itself and asserts a non-empty
span list. `scripts/report_sample.py --verify` re-runs that proof on demand, and
`--check FILE` validates the links in a report `preflight diff` actually wrote,
so this survives a SigNoz upgrade instead of resting on one manual copy.

### `trace_url()` gained an optional third parameter

`contracts.py`-adjacent seams are pinned, and `report.py`'s two signatures were
pinned with them. `trace_url(base_url, trace_id)` is unchanged for all existing
callers; `span_id` was added as an optional third positional with a default, so
the pinned call site in M3 is unaffected. It returns `""` rather than a
half-formed URL when the base or trace id is missing, so the renderer degrades
to plain text instead of emitting a link that goes nowhere.

### New: `PREFLIGHT_SIGNOZ_PUBLIC_URL`

The SigNoz the gate *queries* and the SigNoz a reviewer can *open* are not
always the same host — in CI the former is `http://localhost:8080` inside the
runner. This env var overrides the base URL used for links only. Left unset
(the default, and what the demo uses) links stay on localhost, which is correct
when the person reading the PR is the person running the stack.

### Deviation: CI stands the stack up with `foundryctl forge`, not `cast`

The Makefile's `up` target uses `foundryctl cast -f casting.yaml`, which
validates, generates and starts in one step. The workflow splits it: `forge` to
render `pours/` (gitignored, so it must be generated in-job), then an explicit
`docker compose up -d` followed by a **separate** 600-second health-poll step.
Same deployment from the same committed `casting.yaml`; the split exists so that
container startup — the slowest and most failure-prone thing in the job — fails
in a step whose name says so, with `docker compose ps` and 200 lines of logs
attached, rather than as a confusing timeout three steps later.

### The baseline runs from a detached worktree, not a re-labelled current tree

The premise of the whole project is that the *agent code* changed, so the
baseline suite is executed from `git worktree add --detach <merge-base>`.
Running the current tree twice under two `PREFLIGHT_COMMIT_SHA` labels would
compare a commit against itself and pass forever.

Both runs read the **candidate** checkout's `.cassettes` via
`PREFLIGHT_CASSETTES`. The PR branch is a superset of the base branch's
cassettes, which is what makes replaying an older tree possible at all — and it
keeps CI at exactly zero API calls.

### A gate that cannot run must not look like a gate that failed

`preflight run` exits 2 when ingest never settles; `preflight diff` exits 3 when
there is nothing to compare. Neither means the agent regressed, and CI does not
render them as though it did: each gets its own error annotation, and any path
that produces no `report.md` synthesises one that says *the comparison did not
happen*. The check still goes red — a gate that cannot evaluate is not a pass —
but the PR comment never blames the author for infrastructure.

The comment is posted **before** the check fails, so the diff step deliberately
exits 0 and a later step carries the verdict. Otherwise a breach would fail the
job before the thing that makes the breach legible ever got written.

### Validation done locally, and what could not be

Validated: `actionlint` + `shellcheck` clean; every `run:` block extracted from
the YAML and executed under GitHub's exact `bash --noprofile --norc -eo
pipefail`; the `github-script` body `node --check`ed; and `make ci-local
BRANCH=seeded-regression`, which rehearses the entire Action — merge-base
resolution, two worktrees, both suite runs in replay mode, the gate — and exits
1 with all six deep links resolving.

Not validated locally, and honestly cannot be: the hosted-runner half —
`foundryctl` installing on `ubuntu-latest`, SigNoz reaching health inside the
job's time budget, and the PR-comment upsert against the real API. Those first
run on the demo PR.

> **Gotcha worth carrying forward:** GitHub's default shell is `bash -eo
> pipefail`, so a step that does `cmd | tee log; code=${PIPESTATUS[0]}` dies on
> the failing command before it can capture the exit code it exists to capture.
> Every step here that inspects an exit code says `set +e` first.

---

## M2 — Golden suite + full instrumentation

### The reference agent calls Haiku 4.5 directly, not OpenRouter

`approach.md` planned to reach a model through OpenRouter with an
Anthropic-shaped client, because no first-party key was expected. A key turned
up, so the agent uses the `anthropic` SDK directly and
`claude-haiku-4-5-20251001` is the only model it ever names. That is a downgrade
from the plan's `claude-sonnet-5` for the reference agent, forced by the budget:
the entire project has **$1.00** of credit, Haiku is $1/$5 per MTok, and cost
deltas show just as clearly at that tier — the seeded regression lands at +150%.
Prices for it are in `preflight.yaml` so the cost math stays auditable.

Haiku 4.5 takes neither `effort` nor adaptive `thinking`, so neither is sent.

### Record/replay cassettes, committed to the repo

Not in BUILD_PLAN; added because two separate constraints have the same fix.
The budget cannot survive running a live suite on every iteration, and the
plan's own "golden-suite non-determinism" risk says a regression must not be
sampling noise. `preflight/replay.py` records each request once — keyed by a
hash of (model, system, messages, tools, max_tokens, temperature) — and replays
from `.cassettes/` forever after.

Consequences worth being explicit about:

- **`.cassettes/` is deliberately not gitignored.** A judge cloning the repo
  with no API key can run the full suite; CI runs it for free; the demo is
  byte-reproducible.
- **Replay is the default path**, not a test mode. `PREFLIGHT_REPLAY=1` or a
  missing key forces replay-only, where a cassette miss is a loud `ReplayMiss`
  rather than a silent charge.
- **The suite is now only as fresh as its cassettes.** Re-recording is a
  deliberate act that costs money and can change the six `expect_contains`
  assertions. `temperature=0` is set to keep that drift small — not for
  inference determinism, which it does not provide.

### Cassettes replay the recorded latency

The first version replayed tokens and content and dropped wall time, so a
replayed case finished in ~0.2ms and `p95_latency_ms` became sub-millisecond
noise where ordinary jitter is a +150% swing. Cassettes now store the provider's
measured latency and sleep it on replay, which costs ~20s per suite run and
turns p95 into a real reproducible signal (3.19s baseline vs 8.54s regression).
`PREFLIGHT_FAST_REPLAY=1` skips the wait for iteration.

This is the same failure M3 hit from the other direction and fixed with an
absolute noise floor. Both fixes stay: the floor guards the metric, the recorded
latency makes it meaningful.

### `MAX_MODEL_TURNS` is 5, and the baseline uses 2

A harness ceiling, not a target. At 3, the seeded regression measured +16%
tokens — the "15%, not 3×" case the risk register warns against — because the
cap was clipping the longer trajectory back into looking like baseline. A gate
cannot see a regression its own harness truncates. Raising it changed nothing
for baseline (every case still stops on `end_turn` in two calls; all twelve
cassettes still hit) and let the regression reach +130%.

This does relax the "2 LLM calls per case" instruction, which was a cost
control. The cost control is the ledger, and it held: worst case is 5 calls ×
6 cases on a re-record, and total spend is $0.1123 of $1.00.

### Run-level metrics are all histograms, including `success`

`contracts.py` pins the six names but not their instrument types. All six are
histograms, with `success` recorded as 1.0/0.0, so one query shape serves every
metric and `avg()` over it is the run-level success rate. A counter would not
survive that aggregation.

### The seeded regression is a dependency chain, not a verbosity request

BUILD_PLAN suggests appending something like "enumerate every option before
answering". Measured, that yields **+16%** tokens, not 3×: the model batches its
tool calls into one parallel turn, so more tools do not mean more turns, and the
input context is re-sent the same number of times. What inflates cost is forcing
*sequential* turns — each tool's input derived from the previous tool's output —
which re-sends a growing transcript every turn. Final numbers, off committed
cassettes: cost +150.5%, tokens +129.7%, retrieval hops +133.3%, tool calls
+125.0%, **success rate unchanged at 100%**.

Success staying flat is the point. This is a cost and trajectory regression that
no correctness test can see.

### Metrics emit but do not yet read back through the query API

All six `preflight.case.*` histograms are in ClickHouse with correct
`eval.run_id` / `eval.case_id` / `vcs.commit_sha` labels. SigNoz splits an OTel
histogram into `.bucket` / `.count` / `.sum` / `.min` / `.max` and registers only
`.bucket` in `distributed_metadata` as `type = Histogram`; every
`signal: "metrics"` query attempted through `/api/v5/query_range` returned zero
rows (base name, `.sum` and `.bucket`; with and without
`temporality: Cumulative`; several `spaceAggregation` values). Emit side
verified, read side unresolved, and timeboxed rather than chased: the gate reads
traces through `run_summary_typed`, and M5 builds dashboards through the SigNoz
MCP server rather than hand-rolling these payloads.

Stated plainly so nobody claims "metrics dashboards" in the submission on the
strength of the emit side alone.

### Deviation: the M2 check needed two branches to produce two different totals

The check asks for two runs under two fake SHAs and one query grouped by
`vcs.commit_sha` returning "two rows with plausible, different token totals".
Under cassette replay two runs of the *same* code are byte-identical, so the
totals differ only if the code differs. The check was therefore run as baseline
(`main`) versus the seeded regression, which is the comparison CI actually makes
and closes M3's own check deviation as a side effect. Both rows came back from
one query: 32 spans / 12,201 tokens versus 59 spans / 28,021 tokens.

---

## M5 — Dashboards + alerts as code

### MCP, not REST — and the shortcut paid off

BUILD_PLAN's M5 note says to build dashboards and alerts *through the SigNoz MCP
server* rather than reverse-engineering REST payloads, and flags that "we used
MCP for dashboards" is a submission claim that must be true. **It is true.**
Every dashboard and alert in this repo is created, updated and deleted through
`http://localhost:8000/mcp`; `scripts/signoz_apply.py` makes no REST call at
all. The only REST left in M5 is `make signoz-verify-panels`, which deliberately
re-executes each panel's query through the *same* `/api/v5/query_range` the CI
gate uses — the point of that step is to confirm the dashboards and the gate see
identical numbers, so it has to use the gate's client.

The server exposes **42 tools**, including full dashboard CRUD and alert
create/update/delete, as the plan promised. The payoff was concrete: the tool
arguments are the dashboard objects themselves, so `dashboards/*.json` is the
literal MCP argument rather than a shape this repo has to translate. There is no
adapter layer that can drift.

`scripts/mcp_client.py` is a ~100-line hand-rolled client rather than the `mcp`
SDK. The apply path needs exactly three methods — `initialize`, `tools/list`,
`tools/call` — and the SigNoz server is stateless (it returns no
`Mcp-Session-Id`), so there is no session to manage. It lives under `scripts/`
because **M6 owns `preflight/mcp.py`**; if that lands with an equivalent client,
this one can be deleted and `signoz_apply.py` re-pointed at it.

> The plan warned that the server streams and a naive blocking `curl` hangs. In
> practice it content-negotiates: send `Accept: application/json,
> text/event-stream` and it answers with plain JSON. `_parse()` handles both
> anyway, because "it happens to answer JSON today" is not a transport contract.

### Idempotency has to be client-side: `name` is not a unique key

This is the load-bearing finding for `make signoz-apply`. A dashboard has a
top-level `name` slug that looks exactly like an idempotency key, and is not
one: **posting the same payload twice creates two dashboards** with the same
`name` and different UUIDs. Verified directly.

So apply reconciles rather than upserts — list what exists, match on the stable
identity field, update in place, and delete stale duplicates so the deployment
converges on git instead of accumulating copies:

| Object | Matched on | Update needs |
|---|---|---|
| dashboard | top-level `name` slug | `id` |
| alert rule | `alert` title | `id` |

**Gotcha inside the gotcha:** `signoz_list_alert_rules` returns the rule UUID as
**`ruleId`**, while `signoz_create_alert` returns it as `id` and
`signoz_update_alert` expects `id`. Reading only `id` off a listing yields
`None`, and the update then targets nothing while still reporting success.
`Reconciler._rule_id()` accepts both, and apply now refuses to proceed rather
than silently creating a duplicate if a remote rule has no id at all.

### Four payload shapes the MCP tool schema does not describe

The published `inputSchema` is a faithful description of the Go types and still
does not tell you these. All four were 400s, found by probing:

1. **`schemaVersion` must be `"v6"`.** The schema says `"type": "string"` with no
   enum; `"v2"` is rejected outright.
2. **The grid is 12 columns wide in total, not 24.** A half-width panel is
   `width: 6`. `x: 12` fails with *"x (12) must be less than grid width 12"*.
3. **A table panel formats per column.** Its formatting block takes
   `columnUnits` (alias → unit); a bare `unit` is *"json: unknown field"*. Only
   non-table panels take `unit`.
4. **A panel accepts exactly one query entry.** Sending `A`, `B` and a formula as
   three entries fails with *"panel must have one query"*. Multi-query maths goes
   inside a single `signoz/CompositeQuery` plugin whose envelope list carries the
   input `builder_query` specs plus a `builder_formula`. This is how the headline
   cost-per-task panel computes LLM cost ÷ case count.

### The dashboards read traces, not metrics — deliberately, and not because metrics are broken

M2 left "metrics emit but do not read back" as an open item and handed it to M5.
**M5 cracked it** (see learnings.md for the recipe: query the `.sum` / `.count`
series with an explicit `metricType: "gauge"` and an explicit `timeAggregation`,
because letting SigNoz auto-detect `Histogram/Cumulative/monotonic` is what
returns zero rows). The result cross-checks *exactly* against the trace-derived
total — $0.0769 both ways for commit `3ece0367`.

The dashboards still read traces anyway. Two reasons, and neither is "metrics
don't work":

- **The gate reads traces.** `query.run_summary_typed` is the source of truth for
  the number that fails someone's PR. A dashboard sourced from the same spans
  cannot disagree with the PR comment; one sourced from a parallel metrics
  pipeline can, and the day it does, the gate loses its credibility.
- **Trace aggregation has no temporality or bucket-alignment traps.** The metrics
  read path works but is sensitive to how one-point-per-case series fall across
  step buckets, which is a subtlety I verified for one query and did not want
  underneath twenty-four panels.

Stated plainly so nobody claims "metrics-backed dashboards" in the submission:
the dashboards are **trace-backed**. The metric names in `contracts.py` are
emitted, and now proven readable, but nothing in M5 depends on them.

### Alert thresholds are measured, not guessed

Same discipline as M3's thresholds. Both rules were sized from real runs of the
suite through real SigNoz:

| | Baseline (`main`) | Seeded regression | Threshold |
|---|---|---|---|
| cost per task | $0.00255 | $0.00550 | **$0.004** (critical) |
| tool error rate | 0% | 0% | **5%** (error) |

The cost rule therefore fires on the regression and stays quiet on `main` —
verified live, the rule sat in `firing` while the tool-error rule sat in
`inactive`. Grouping by `vcs.commit_sha` means the notification names the commit.

**The tool-error rule is quiet, and that is the correct outcome, not a gap.** No
tool span has ever errored in this project. It is expressed as a *rate* rather
than a raw count because one flaky call in a large run is noise and one in five
is a broken tool. `make signoz-verify-panels` has an explicit `ALLOW_EMPTY`
entry for the matching dashboard panel, with the reason written down, so "no
rows" stays a hard failure everywhere else instead of becoming a shrug.

### New: a notification channel is a prerequisite, so apply creates it

SigNoz rejects an alert rule that references an unknown notification channel, so
`signoz_apply.py` ensures `preflight-local-webhook` exists before applying the
rules. It points at `host.docker.internal:9099`, where nothing listens: the rules
evaluate and fire correctly, they simply cannot deliver. A real deployment
repoints it at Slack or PagerDuty. SigNoz test-pings a webhook on create and
reports the failure in the result; that is expected here and is not treated as
fatal.

### The M5 check is automated, and proves more than the plan asked

BUILD_PLAN's check is "delete a dashboard from the UI, run `make signoz-apply`,
it comes back identical". Doing that by hand proves it once.
`make signoz-apply-check` proves it on demand, and proves the stronger claim —
not that *a* dashboard reappears, but that the definition which comes back is
byte-identical to the one destroyed, ignoring only the fields the server owns
(UUID and timestamps). It then applies a third time and asserts nothing was
created, which is the idempotency half. Output in learnings.md.

### Not done in M5

- **No `signoz-apply` step in CI.** The workflow stands up a fresh SigNoz per
  job, so applying dashboards there would decorate a deployment that is torn
  down minutes later. Apply is a local/operator command. Wiring it into CI is a
  one-line addition if a persistent SigNoz ever exists.
- **`webUrl` values are not usable from a browser as returned.** The MCP server
  builds them from the container-internal hostname
  (`http://signoz-signoz-0:8080/dashboard/<uuid>`). The MCP server's own
  instructions say to use `webUrl` verbatim and never construct links — good
  advice that happens to be wrong for a Foundry compose deployment reached from
  the host. Swap the host for `localhost:8080`; the UUID path is correct.
- **Dashboard JSON is hand-maintained.** It was authored with a throwaway
  generator for consistency, which was not kept: a committed generator would
  make the JSON a build artifact and give the repo two sources of truth for the
  same object. `make signoz-verify-panels` is what catches a hand-edit mistake.
