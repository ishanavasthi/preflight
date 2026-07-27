# Approach

## Architecture in one paragraph

Python. The reference agent is instrumented with the OpenTelemetry SDK —
hand-rolled spans following the GenAI semantic conventions, so attribute names
are exactly what the spec says rather than whatever an auto-instrumentation
library emits. Every run of the golden suite emits one trace per case, tagged
`eval.run_id` / `eval.case_id` / `vcs.commit_sha`, plus run-level metrics on the
same dimensions. The differ queries SigNoz for per-run aggregates on the
candidate SHA and the baseline SHA and compares them.

## Key design decisions

### SigNoz is the datastore for the gate, not a dashboard bolted on the side

There is no local results file. Every number in a PR comment came out of
`POST /api/v5/query_range`. This is the design bet and also the "Best Use of
SigNoz" pitch.

It rested on one load-bearing question — *can the SigNoz query API aggregate and
group by a custom span attribute?* **Verified yes** against v0.134.0.
`groupBy: [{name: "eval.run_id", fieldContext: "attribute"}]` returns one row per
run, and multi-aggregation works in the same round trip.

*Fallback if it had failed:* the differ reads a local artifact for the gate and
SigNoz keeps traces/dashboards/alerts for the diagnosis half. Not needed.

### Novelty budget: spent on nothing

OTel SDK, OTLP over HTTP, a REST client, arithmetic, a GitHub Action. Every risk
in this project is integration risk, not technology risk — the right shape for
one person and one night. This is deliberate, not a shortcut.

### Hand-rolled spans over auto-instrumentation

The gate reads spans back *by attribute name*, so the names must be exactly the
spec's. Auto-instrumentation would put a library's naming choices between the
agent and the gate. Cost: more code. Benefit: the query layer is stable and the
attribute table in the README is honest.

Every `gen_ai.*` attribute used is at **Development** stability in the spec. Said
so in the README — it reads as rigor and inoculates against a judge noticing churn.

### Fail loudly on partial ingest rather than diff a half-written run

CI writes traces and immediately reads aggregates back, with an exporter flush,
collector batching, and a ClickHouse insert in between. Reading too early yields
a *silently wrong* diff — the worst failure mode in the project, because nothing
looks broken.

So the runner polls `count()` filtered on `eval.run_id` until it matches the
expected span count, and raises on timeout. Observed lag is 2–4s; the timeout is
120s. The rule when it trips is to **raise the timeout and say so in the README**,
never to shorten it to look fast.

### Model access: planned for OpenRouter, shipped on a first-party Anthropic key

The plan assumed no Anthropic key on the build machine, so the design was to
point an Anthropic-shaped client at OpenRouter by overriding the base URL —
keeping `gen_ai.provider.name` / `gen_ai.request.model` semantics intact and
making a swap a one-line change. **A first-party key arrived before M2 started,
so the base-URL override was never needed** and the agent talks to
`api.anthropic.com` directly on `claude-haiku-4-5-20251001`. The decision is kept
here because the *shape* is what mattered: not rewriting the agent around another
provider's SDK is why swapping the endpoint would have been one line either way.

Prices for whatever model is actually used live in `preflight.yaml` so the cost
math stays auditable against a committed source.

### Deterministic-by-default agent

BUILD_PLAN's own risk register flags golden-suite non-determinism as a threat to
the demo reproducing on stage. Mitigation is twofold: set thresholds well above
observed run-to-run variance, and make the seeded regression large (3×, not 15%).

## Tradeoffs and alternatives considered

| Decision | Alternative | Why not |
|---|---|---|
| SigNoz as source of truth | Local JSON artifact for the gate | Weaker submission; the whole SigNoz pitch collapses. Kept only as a kill-criteria fallback. |
| Hand-rolled spans | OpenLLMetry / Traceloop / Langtrace | Attribute names become a library's choice; the gate queries by name. |
| OrbStack | Docker Desktop, colima | No runtime existed. Picked for startup speed and RAM on a laptop running the full stack. |
| Dashboards/alerts via SigNoz MCP | Reverse-engineer the REST payloads | MCP has verified dashboard CRUD and alert-rule CRUD; faster, removes an unverified unknown, and is itself MCP usage to point at. |

## Kill criteria

- **M1 check fails after 2 hours** → SigNoz-as-source-of-truth dies, fall back to
  a local artifact for the gate. *(Not triggered — M1 passed.)*
- **M3 takes >2× its estimate** → cut trajectory-divergence and retrieval-hop
  metrics, gate on cost + latency + success rate only. Three metrics that work
  beat six that don't.
- **Past 06:00 and M4 not green** → stop building, start M7. The video, blog, and
  form are hard requirements; M5 and M6 are multipliers on a submission that must
  exist first.
