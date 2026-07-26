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
