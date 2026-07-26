# Implementation

## Milestone status

| # | Milestone | Status |
|---|---|---|
| M1 | Walking skeleton — Foundry up, one instrumented trace, read back via query API | **Done** — check passed |
| M2 | Golden suite + full instrumentation | Not started |
| M3 | Differ + gate | Not started |
| M4 | GitHub Action + PR comment (*demo complete here*) | Not started |
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

**Query layer.** `preflight/query.py` wraps `POST /api/v5/query_range` with a
scalar builder-query helper supporting aggregations, filter expressions, and
group-by on custom span attributes.

**Harness.** `preflight/runner.py` runs the suite and then blocks in
`wait_for_ingest` until SigNoz reports the expected span count, raising
`IngestTimeout` rather than allowing a partial diff.

**CLI.** `preflight run | query | raw`. `raw` is a debugging escape hatch for
arbitrary scalar queries.

**Bootstrap.** `scripts/bootstrap_signoz.sh` creates the first admin user, mints
an admin-scoped service-account key, verifies it, and writes `.env`.

## Integrations

| Integration | Where | Notes |
|---|---|---|
| SigNoz query API v5 | `preflight/query.py` | Source of truth for the gate |
| OTLP/HTTP | `preflight/otel.py` | Traces + metrics to `:4318` |
| Foundry | `casting.yaml` | Deployment reproducibility (Field Req 3) |
| SigNoz MCP server | port 8000, enabled | Wired up; used in M5/M6 |
| OpenRouter | M2 | Anthropic-shaped client with base-URL override |
| GitHub Actions | M4 | `gh` CLI authed as `ishanavasthi` |

## Attributes emitted

GenAI semconv (all **Development** stability): `gen_ai.operation.name`,
`gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.tool.name`,
`gen_ai.tool.call.id`.

Preflight's own: `eval.run_id`, `eval.case_id`, `vcs.commit_sha`,
`preflight.cost_usd`, `preflight.span_role`, `preflight.duration_ms`.

## Known gaps

- Run-level **metrics** are wired but nothing emits them yet (M2).
- **Logs with trace context** not started (M2).
- SigNoz **trace deep-link URL format** still unresolved — to be copied from the
  UI address bar when `report.py` is written (M4), not guessed.
- The reference agent is a deterministic stub; real model calls land in M2.
