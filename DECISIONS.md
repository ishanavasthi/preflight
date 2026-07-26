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
