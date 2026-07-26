# Preflight

**Your unit tests can't see a 3× cost regression. This can.**

A team ships an AI agent behind a PR workflow. Someone edits one line of a
prompt, swaps a tool description, or bumps a model. The tests still pass — the
code didn't change shape. What changed is invisible: the agent now makes two
extra retrieval hops, reaches for a different tool, and costs 3× per task.
Nobody sees it at review time. They see it in next month's invoice, or in a
latency alert at 2am.

Preflight catches it on the PR that caused it, and links the specific span that
explains it.

> **Status: M1 (walking skeleton) complete.** The telemetry round-trip works
> end to end — the agent emits spans, SigNoz ingests them, and the CLI reads
> per-case aggregates back through the query API. The differ, the gate, and the
> GitHub Action are M3–M4. See `BUILD_PLAN.md` for the milestone plan and
> `DECISIONS.md` for what changed along the way.

---

## The design bet

**SigNoz is the datastore for the gate, not a dashboard bolted on the side.**

Every run of the golden suite emits one trace per case, tagged `eval.run_id`,
`eval.case_id`, and `vcs.commit_sha`. The differ then asks SigNoz for per-run
aggregates on the candidate commit and the baseline commit and compares them.
There is no local results file to fall back on — every number in a PR comment
came out of `POST /api/v5/query_range`.

That bet rested on one question: *can the SigNoz query API aggregate and group
by a custom span attribute?* It can — verified against v0.134.0, details in
`DECISIONS.md`.

## Quickstart

Needs a container runtime (`docker compose`) and [`uv`](https://docs.astral.sh/uv/).

```bash
foundryctl cast -f casting.yaml   # or: make up   -- brings up SigNoz + its MCP server
make bootstrap                    # admin user + API key -> .env
make install                      # sync the Python env
make check                        # run one case, read it back out of SigNoz
```

`make check` is the M1 acceptance test. It prints a span count that came from
the HTTP API for a run created seconds earlier:

```
run_id      run-4f246091f756
commit_sha  c9e2e131d2b5fda8caae10139b236cacc20a2169
cases       2
spans       11 expected

waiting for SigNoz ingest...
OK: 11 spans queryable in SigNoz.

run_id      run-4f246091f756
spans       11   (source: SigNoz /api/v5/query_range)

case                      spans   in_tok  out_tok   cost_usd
------------------------------------------------------------
order-status                  5      982      408   0.006040
refund-policy                 6     1754      552   0.009030
```

SigNoz UI: <http://localhost:8080>. MCP server: `http://localhost:8000/mcp`.

## Why the ingest poller exists

CI writes traces and immediately wants to read aggregates back, with an exporter
flush, collector batching, and a ClickHouse insert in between. Reading too early
yields a partial run and a *silently wrong* diff — the worst failure mode in the
project, because nothing looks broken.

So `preflight run` polls `count()` filtered on `eval.run_id` until it matches the
expected span count, and **fails loudly on timeout** rather than letting the
differ proceed. Observed lag on this deployment is 2–4s; the timeout is 120s.

## Instrumentation

Hand-rolled OpenTelemetry spans, not auto-instrumentation — the gate reads spans
back *by attribute name*, so the names have to be exactly what the spec says.

| Attribute | Source |
|---|---|
| `gen_ai.operation.name`, `gen_ai.provider.name` | GenAI semconv |
| `gen_ai.request.model`, `gen_ai.response.model` | GenAI semconv |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | GenAI semconv |
| `gen_ai.tool.name`, `gen_ai.tool.call.id` | GenAI semconv |
| `eval.run_id`, `eval.case_id`, `vcs.commit_sha` | Preflight |
| `preflight.cost_usd` | Computed from the price table in `preflight.yaml` |

> Every `gen_ai.*` attribute above is at **Development** stability in the
> OpenTelemetry spec. Names may still churn between spec releases; if a query
> stops matching, check the spec version first.

## Reproducing the deployment

`casting.yaml` and `casting.yaml.lock` are committed. `foundryctl cast -f
casting.yaml` brings up SigNoz *and* its MCP server (`spec.mcp.spec.enabled:
true`, port 8000). The rendered compose output under `pours/` is generated, not
committed.

## Layout

```
casting.yaml / casting.yaml.lock   Foundry -- judges re-run this
preflight.yaml                     thresholds + model price table
agent/reference.py                 the agent under test
preflight/otel.py                  tracer + meter + OTLP exporter
preflight/instrument.py            llm_span() / tool_span() / retrieval_span()
preflight/runner.py                golden-suite harness + ingest poller
preflight/query.py                 SigNoz /api/v5/query_range client
preflight/cli.py                   preflight run | query | raw
suite/cases/*.yaml                 the golden suite
scripts/bootstrap_signoz.sh        admin user + API key -> .env
```

## AI assistance

This project was built with **Claude Code** (Claude Opus 5) as a coding
assistant, disclosed per Agency Protocol 7.
