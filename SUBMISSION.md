# Submission

Draft answers for the [project submission form](https://forms.gle/xv1TXSiC54MEWujRA).

**Rule applied throughout:** every claim below is true of what is actually in the
repo, and was checked against the code rather than the plan. BUILD_PLAN's M7
instruction — *"Delete any bullet you didn't reach — judges re-run Foundry and
look"* — is the standard here. Two things we did *not* reach are stated as gaps
rather than quietly dropped.

---

## Project name

**Preflight — catch AI agent regressions in the pull request that caused them**

## Track

Track 01 — AI & Agent Observability

## Team

Solo: ishanavasthi

## Repository

<https://github.com/ishanavasthi/preflight>

Includes `casting.yaml` and `casting.yaml.lock` per Field Requirement 3.

## Deployed link

None. The interface is the PR comment and the SigNoz dashboards; a hosted URL
would not show anything the repo doesn't. The form marks this optional.

---

## One-line description

Preflight runs a golden suite of agent tasks on every pull request, compares
cost, tokens, latency and tool trajectory against the merge base **entirely
through SigNoz's query API**, and fails the check with a comment naming the
metric, the delta, and the span that explains it.

## The problem

Someone edits one line of a prompt. The unit tests still pass — the code didn't
change shape. What changed is invisible to every tool in the pipeline: the agent
reaches for a different tool, makes two extra retrieval hops, and costs 3× per
task. Nobody sees it at review time. They see it in next month's invoice, or in a
latency alert at 2am.

Existing CI has no opinion about agent behaviour because behaviour isn't in the
diff. Preflight makes it a first-class, gated signal.

## What it does

On every PR it runs a 6-case golden suite against both the merge base and the PR
head, emitting one OpenTelemetry trace per case with GenAI semantic-convention
attributes. It then queries SigNoz for per-commit aggregates, compares six
metrics against thresholds committed in `preflight.yaml`, and exits non-zero on
breach. A sticky PR comment carries the delta table and a deep link per case.

When the gate fails, a diagnosis agent investigates the traces **over the SigNoz
MCP server** and explains the regression in English — and its own investigation is
itself a trace in SigNoz.

---

## How we used SigNoz

**SigNoz is the datastore for the gate, not a dashboard bolted on the side.**
That is the design decision the whole project rests on. There is no local results
file: every number in a PR comment came back out of `POST /api/v5/query_range`.
If SigNoz can't answer, the gate fails loudly rather than guessing.

**Traces.** Hand-rolled OTel spans following the GenAI semantic conventions
(`gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`,
`gen_ai.response.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.tool.name`, `gen_ai.tool.call.id`) plus our
own `eval.run_id` / `eval.case_id` / `vcs.commit_sha` / `preflight.cost_usd`.
Hand-rolled on purpose: the gate reads spans back *by attribute name*, so the
names must be exactly the spec's rather than whatever a library emits.

**Query Builder v5.** The gate is built on `aggregations[]` + `filter.expression`
+ `groupBy[]` with explicit `fieldContext`, grouping by custom span attributes to
get per-case, per-commit rows in one round trip. Verifying that this was possible
was the first thing we built, because a "no" would have killed the design.

**Metrics.** Run-level metrics on the same dimensions as the spans
(`preflight.case.cost_usd`, `.tokens`, `.duration_ms`, `.tool_calls`,
`.retrieval_hops`, `.success`).

**Logs.** Emitted with trace context attached, so a log line resolves to its span.

**Dashboards — as code, applied through MCP.** Four committed dashboards (agent
health, cost-per-task by commit, tool-trajectory breakdown, per-case latency) in
`dashboards/`, applied idempotently by `make signoz-apply`. `make
signoz-verify-panels` executes every committed panel query and fails if one
returns no data, so a dashboard cannot silently rot.

**Alerts — as code.** Two rules in `alerts/`: cost-per-task burn rate and
tool-error rate, applied by the same command.

**MCP server.** Used for two distinct things. First, dashboards and alerts are
created and reconciled *through MCP* rather than through reverse-engineered REST
payloads. Second, the diagnosis agent uses MCP as its only source of facts —
`signoz_aggregate_traces` and `signoz_get_trace_details` — to investigate a failed
gate.

**Full circle.** The diagnosis agent is instrumented into SigNoz itself. A single
failed gate produces: the agent-under-test's traces, the gate's queries against
them, and a 22-span trace of the diagnosis agent investigating — all in one
place. `make m6-check` reproduces this offline and prints the trace URL.

### Things we did not reach

- The diagnosis is **not** appended to the PR comment automatically; it runs via
  `make explain`. The wiring was deliberately frozen while the demo PR was live.
- Chaos/fault injection, multi-language support, and statistical significance
  testing were all cut in the plan and stayed cut.

---

## What makes it non-obvious

**The gate has to read its own writes.** CI writes traces and immediately queries
aggregates, with an exporter flush, collector batching and a ClickHouse insert in
between. Reading too early produces a *silently wrong* diff — the worst failure
mode available, because nothing looks broken. So the runner polls until the run's
spans are queryable and **fails loudly on timeout** rather than diffing a partial
run.

That guard paid for itself before it ever ran in CI: it caught a bug in our own
response parser by reporting `0/6` spans instead of quietly returning an empty
list that downstream code would have read as "no regression."

---

## AI assistance

Built with **Claude Code** (Claude Opus 5) as a coding assistant, disclosed per
Agency Protocol 7. Planning and design were written before kickoff per Protocol
8; `BUILD_PLAN.md` is committed unedited and every deviation from it is logged in
`DECISIONS.md`.

---

# Video plan (≤ 3 minutes)

Structure per BUILD_PLAN: **problem → architecture → the demo → what you
learned.** Record `make ci-local` for anything involving a click.

> ⚠️ **Do not click a deep link from the GitHub PR comment on camera.** CI stands
> up its own SigNoz inside the runner and tears it down when the job ends, so
> those trace IDs no longer resolve. `make ci-local` produces the same comment
> against your local stack, where every link opens. Verified.

| # | ~Time | Shot | Say |
|---|---|---|---|
| 1 | 0:00–0:25 | The PR diff on GitHub — nine lines of a system prompt. Scroll to the green unit-test check. | "Someone made the agent more thorough. Tests pass. Looks harmless." |
| 2 | 0:25–0:50 | Scroll down to the red Preflight check and its comment. Let the table sit on screen. | "It costs 150% more per task, makes twice the retrieval hops, and reaches for a tool it never used before. None of that is in the diff." |
| 3 | 0:50–1:20 | One diagram or a fast `preflight/` file scroll: suite → OTel spans → SigNoz → query API → gate. | "Every number in that comment came out of SigNoz's query API. There's no results file — SigNoz *is* the datastore." |
| 4 | 1:20–1:50 | Terminal: `make ci-local BRANCH=seeded-regression`. Show it exit non-zero, then **click a per-case trace link** → SigNoz waterfall. | "Same gate, locally. And the link goes straight to the span that explains it." |
| 5 | 1:50–2:20 | `make m6-check`. Show the diagnosis text, then open the trace URL it prints. | "The diagnosis agent investigates over MCP — and its own investigation is a trace in SigNoz. An agent debugging an agent, both observable in the same place." |
| 6 | 2:20–2:40 | SigNoz dashboards: cost-per-task by commit. | "Dashboards and alerts are committed JSON, applied through MCP." |
| 7 | 2:40–3:00 | Back to the red check. | "Caught on the PR that caused it, with the span one click away. That's the whole idea." |

**Pre-flight checklist before recording**

```bash
make up                     # stack healthy
make signoz-apply           # dashboards present for shot 6
make ci-local BRANCH=seeded-regression   # warms traces; links will resolve
make m6-check               # confirm it passes and note the trace URL
```

Shot 5's trace URL changes on every run — read it off the terminal, don't
pre-write it.
