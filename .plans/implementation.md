# Implementation

## Milestone status

| # | Milestone | Status |
|---|---|---|
| M1 | Walking skeleton — Foundry up, one instrumented trace, read back via query API | **Done** — check passed |
| M2 | Golden suite + full instrumentation | **Done** — check passed (two SHAs, one query grouped by `vcs.commit_sha`, two rows with different token totals) |
| M3 | Differ + gate | **Done** — check passed (exit 1 with the cost delta named, exit 0 on a clean re-run; regression proven with synthetic runs through real SigNoz, see DECISIONS.md) |
| M4 | GitHub Action + PR comment (*demo complete here*) | **Built** — validated locally end to end (`make ci-local` exits 1, all 6 deep links resolve); awaiting the real PR, which the human opens |
| M5 | Dashboards + alerts as code | Not started |
| M6 | Diagnosis agent over MCP | Not started |
| M7 | Ship — README, video, blog, submission form | Not started |

## What is built

**Deployment.** SigNoz v0.134.0 via Foundry (`foundryctl` v0.2.16) on OrbStack.
`casting.yaml` enables the MCP server (`spec.mcp.spec.enabled: true`, port 8000).
`casting.yaml` and `casting.yaml.lock` are committed; the rendered `pours/` output
is generated and gitignored.

**Telemetry.** `preflight/otel.py` installs a tracer and meter provider with an
OTLP/HTTP exporter and a resource carrying `service.name` and `vcs.commit_sha`.
`preflight/instrument.py` provides `case_span` / `llm_span` / `tool_span` /
`retrieval_span`, each stamping the eval dimensions. Cost is computed inside
`llm_span` on exit from the price table, so `preflight.cost_usd` and the token
attributes can never disagree.

**Reference agent.** `agent/reference.py` is a real tool-calling loop against
`claude-haiku-4-5-20251001` over the fake dataset in `agent/data.py`: one
grounding retrieval hop, then up to five model turns (baseline uses two) with
three tools — `lookup_order`, `policy_search`, `check_inventory`. `policy_search`
nests a retrieval span, so it costs a hop. Six cases in `suite/cases/`, 5–13
spans each, 32 spans per baseline run. The M1 deterministic stub is still
reachable behind `PREFLIGHT_AGENT=stub` for testing telemetry without a model.

**Record/replay cache.** `preflight/replay.py` keys each request by a hash of
(model, system, messages, tools, max_tokens, temperature) and stores the
response as JSON under `.cassettes/`, which is **committed on purpose**.
`PREFLIGHT_REPLAY=1` — or a missing `ANTHROPIC_API_KEY` — replays and never
calls the API; a miss in that mode raises `ReplayMiss` rather than spending.
Cassettes also record the provider's wall time and replay it, so `p95_latency_ms`
is a real reproducible number instead of sub-millisecond noise
(`PREFLIGHT_FAST_REPLAY=1` skips the wait). On a miss with a key present, the
call is gated by `preflight.budget.check()` and its actual cost recorded.

**Metrics.** `instrument.emit_case_metrics()` records all six
`contracts.METRIC_CASE_*` names on the `eval.run_id` / `eval.case_id` /
`vcs.commit_sha` dimensions. All six are histograms — including `success`, as
1.0/0.0 — so one query shape serves every metric and `avg()` yields the
run-level success rate.

**Logs.** `otel.py` installs a `LoggerProvider` with an OTLP exporter and
attaches the SDK `LoggingHandler` to the `preflight` logger, so every line the
agent writes carries the active span's `trace_id` and `span_id`.

**Query layer.** `preflight/query.py` wraps `POST /api/v5/query_range` with a
scalar builder-query helper supporting aggregations, filter expressions, and
group-by on custom span attributes.

**Harness.** `preflight/runner.py` runs the suite and then blocks in
`wait_for_ingest` until SigNoz reports the expected span count, raising
`IngestTimeout` rather than allowing a partial diff.

**Differ + gate.** `preflight/differ.py` resolves each commit SHA to its most
recent `eval.run_id` (one scalar query grouped by `eval.run_id`, aggregating
`count()` and `max(timestamp)`), reads both runs through
`query.run_summary_typed`, and computes every metric in
`contracts.GATED_METRICS` via `RunSummary.metric()` — the differ deliberately
does none of the metric arithmetic itself, so it and `report.py` cannot disagree
about what "cost per task" means. Thresholds come from `preflight.yaml`.
Percentage rise for the five higher-is-worse metrics, absolute drop for
`success_rate`, plus an absolute noise floor on latency. Runs with different
case sets are compared on their intersection and say so in `DiffReport.notes`.
`DiffError` (nothing comparable) is kept strictly separate from a breach.

**CLI.** `preflight run | query | diff | raw`. `raw` is a debugging escape hatch
for arbitrary scalar queries. `diff --baseline <sha> --candidate <sha>` takes
`--format markdown|json`, `--output <path>` and `--lookback-minutes`, and its
exit codes are part of the interface: **0** clean, **1** a gated metric
breached, **2** ingest timed out, **3** SigNoz unreachable or nothing to
compare. 1 and 3 are deliberately distinct — an expired baseline must never
render in CI as a broken agent.

**Bootstrap.** `scripts/bootstrap_signoz.sh` creates the first admin user, mints
an admin-scoped service-account key, verifies it, and writes `.env`.

**PR comment.** `preflight/report.py` renders a `DiffReport` as the comment:
pass/fail banner, a headline naming the breached metrics worst-first, the delta
table, a "biggest mover" line pointing at the offending trace, and a collapsed
per-case breakdown where every row deep-links to its own trace. The link format
is `{base}/trace/{trace_id}[?spanId={span_id}]`, **verified against SigNoz
v0.134.0 four ways** (router constant, the `spanId` query-param reader, the UI's
own Copy-link handler, and the `/api/v4/traces/{id}/waterfall` API) — see
DECISIONS.md. `PREFLIGHT_SIGNOZ_PUBLIC_URL` overrides the link host when the
SigNoz CI queries is not the one a reviewer can open. `MARKER` is the anchor the
Action's sticky-comment upsert greps for.

**CI gate.** `.github/workflows/preflight.yml` runs on `pull_request`: forge
SigNoz in-job from the committed `casting.yaml` (`pours/` is gitignored) →
`docker compose up` → a separate 600s health poll → `bootstrap_signoz.sh` →
baseline from `git merge-base origin/$GITHUB_BASE_REF HEAD` → suite on the merge
base in a detached worktree and on the PR head → `preflight diff --format
markdown --output report.md` → sticky comment upsert (`pull-requests: write`) →
fail the check. `PREFLIGHT_REPLAY=1` throughout, so CI makes **zero** model API
calls. Exit 2 and 3 are surfaced as "the gate could not run", never as a
regression verdict.

**Local rehearsal.** `make ci-local BRANCH=<branch>` runs the entire Action
sequence on a laptop and exits with the gate's code; `make verify-links` proves
the deep links resolve; `make lint-ci` runs actionlint + shellcheck.

## Integrations

| Integration | Where | Notes |
|---|---|---|
| SigNoz query API v5 | `preflight/query.py` | Source of truth for the gate |
| OTLP/HTTP | `preflight/otel.py` | Traces, metrics **and logs** to `:4318` |
| Foundry | `casting.yaml` | Deployment reproducibility (Field Req 3) |
| SigNoz MCP server | port 8000, enabled | Wired up; used in M5/M6 |
| Anthropic API | `preflight/replay.py` | `anthropic` SDK, `claude-haiku-4-5-20251001` only. Every call gated by `preflight/budget.py` against a $1 cap; total M2 spend $0.1123 / 69 calls |
| Cassette replay | `.cassettes/` (committed) | 12 baseline + 21 on `seeded-regression`. Default execution path — CI and a fresh clone run the suite offline and free |
| GitHub Actions | M4 | `gh` CLI authed as `ishanavasthi` |

## Attributes emitted

GenAI semconv (all **Development** stability): `gen_ai.operation.name`,
`gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.tool.name`,
`gen_ai.tool.call.id`.

Preflight's own: `eval.run_id`, `eval.case_id`, `vcs.commit_sha`,
`preflight.cost_usd`, `preflight.span_role`, `preflight.duration_ms`.

## Known gaps

- **Metrics are emitted but not yet readable through the query API.** All six
  `preflight.case.*` histograms land in ClickHouse with the right names and
  labels, but SigNoz splits histograms into `.bucket` / `.count` / `.sum` /
  `.min` / `.max` and registers only `.bucket` as `type = Histogram`. Every
  `signal: "metrics"` query attempted through `/api/v5/query_range` returned
  zero rows. Read-path shape only — the emit side is verified. Belongs to M5,
  which builds dashboards through the SigNoz MCP server anyway. **The gate does
  not depend on it:** M3 reads traces via `run_summary_typed`.
- **Tool-call trajectory divergence** in the sequence sense is not implemented —
  `tool_calls_per_task` counts calls, it does not notice a reordering. Say
  "tool-call volume" in the submission.
- `seeded-regression` is **not merged and must not be**. It exists for M4's PR.
