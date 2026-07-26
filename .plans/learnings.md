# Learnings

Newest first.

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
