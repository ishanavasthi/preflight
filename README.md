# Preflight

**Someone edits one line of a prompt. The tests still pass. The agent now costs 3× per task.**

Nothing about that change looks dangerous at review time. The diff is a sentence.
CI is green because the code didn't change shape — same functions, same
signatures, same assertions. What changed is invisible to every tool in the
pipeline: the agent now reaches for a different tool, makes two extra retrieval
hops, and burns three times the tokens on every single task.

You find out in next month's invoice. Or in a latency alert at 2am. Or when a
user complains.

**Preflight catches it on the pull request that caused it, and links the exact
span that explains why.**

---

## This is the real thing, not a mockup

PR [#1](https://github.com/ishanavasthi/preflight/pull/1) changes nine lines of a
system prompt. Unit tests pass. Here is what the gate posted, on GitHub Actions:

> ## ❌ Preflight — regression detected
>
> `e0592cf8` → `59607e52` · 6 cases
>
> > **p95 latency ▲ +167.4%** (limit +75%), **Cost / task ▲ +150.5%** (limit +25%), **Retrieval hops / task ▲ +133.3%** (limit +50%), **Tokens / task ▲ +129.7%** (limit +25%), **Tool calls / task ▲ +125.0%** (limit +40%)
>
> | | Metric | Baseline | Candidate | Δ | Threshold |
> |:--:|---|---:|---:|---:|---:|
> | ❌ | Cost / task | $0.0026 | $0.0064 | ▲ +150.5% | +25% |
> | ❌ | Tokens / task | 2,034 | 4,670 | ▲ +129.7% | +25% |
> | ❌ | p95 latency | 3.18s | 8.51s | ▲ +167.4% | +75% |
> | ❌ | Tool calls / task | 1.33 | 3 | ▲ +125.0% | +40% |
> | ❌ | Retrieval hops / task | 1 | 2.33 | ▲ +133.3% | +50% |
> | ✅ | Success rate | 100% | 100% | — | drop > 1 pts |
>
> **Biggest mover:** `damaged-item` moved $0.0070 (+5,100 tokens, +3 tool calls) — open the trace in SigNoz
>
> <sub>Every number above came from SigNoz `/api/v5/query_range`.</sub>

The check fails. The comment names the metric, the delta, and the case. Each row
links to that case's trace waterfall.

---

## The design bet: SigNoz is the datastore, not a dashboard bolted on the side

There is no local results file. Every number in that comment came back out of
`POST /api/v5/query_range`. The suite writes traces; the gate reads aggregates.
If SigNoz can't answer, the gate doesn't guess — it fails loudly.

That bet rested on one question, which had to be answered before any of this was
worth building: **can the SigNoz query API aggregate and group by a custom span
attribute?** It can.

```jsonc
"aggregations": [{"expression": "sum(preflight.cost_usd)"}],
"filter":       {"expression": "attribute.vcs.commit_sha = '59607e52…'"},
"groupBy":      [{"name": "eval.case_id", "fieldContext": "attribute"}]
```

One row per case, per commit, in one round trip. Everything else follows from
that.

---

## Quickstart

Needs a container runtime (`docker compose`) and [`uv`](https://docs.astral.sh/uv/).

```bash
foundryctl cast -f casting.yaml   # or: make up   — SigNoz + its MCP server
make bootstrap                    # admin user + API key → .env
make install                      # sync the Python env
make check                        # run a case, read it back out of SigNoz
```

Then, in rough order of how convincing they are:

```bash
make ci-local BRANCH=seeded-regression   # the whole gate, end to end, exits 1
make signoz-apply                        # dashboards + alerts, through MCP
make m6-check                            # the diagnosis agent — no API key needed
```

`make` on its own lists every target.

> **`make m6-check` and `make ci-local` cost nothing.** Model responses are
> recorded as cassettes and committed, so both replay offline and byte-identically
> with `ANTHROPIC_API_KEY` unset — which is the state you're in after cloning.

---

## What's here

| | |
|---|---|
| **Deployment** | SigNoz v0.134.0 via **Foundry**. `casting.yaml` + `casting.yaml.lock` committed; MCP server enabled on `:8000`. |
| **Agent under test** | Multi-tool customer-support agent on `claude-haiku-4-5-20251001` — 3 tools (`lookup_order`, `policy_search`, `check_inventory`), a `policy-kb` retrieval hop, 5–13 spans per case. |
| **Golden suite** | 6 cases in `suite/cases/`, one trace each. |
| **The gate** | `preflight diff --baseline <sha> --candidate <sha>` — six metrics, thresholds in `preflight.yaml`, non-zero exit on breach. |
| **CI** | `.github/workflows/preflight.yml` stands the whole stack up **inside the job**, resolves the baseline from the merge base, and posts a sticky PR comment. |
| **Dashboards & alerts** | 4 dashboards, 2 alert rules, in `dashboards/` and `alerts/`, applied idempotently **through the SigNoz MCP server**. Written against the **v6 dashboard schema**, which renders only under `use_dashboard_v2` — `casting.yaml` turns that flag on, so a fresh `foundryctl cast` gets it. |
| **Diagnosis agent** | Given a failed gate, investigates over MCP and explains it in English — and its own investigation is a 22-span trace in SigNoz. |

### The full circle

The diagnosis agent is the part worth clicking. It receives the gate's verdict,
then drives the SigNoz MCP server — 15 tool calls across 6 turns — to work out
what actually changed:

> The **damaged-item case** is the worst performer, with cost increasing from
> $0.00246 to $0.00946 (+284%), tokens from 2,016 to 7,116 (+253%)… the candidate
> now calls `policy_search` … Retrieval hops to policy-kb doubled from 6 to 14.

Neither `policy_search` nor `policy-kb` appears anywhere in that agent's prompt.
They exist only as span attributes — so quoting them is proof it went to SigNoz
and came back. That is also what `make m6-check` asserts, which is why it is a
real check and not a fluency test.

Its own investigation lands in SigNoz as a 22-span trace under service
`preflight-diagnose` — a root `diagnose regression` span, its LLM turns, and one
span per MCP call. `make m6-check` prints the trace URL it just produced; open
it.

An agent debugging an agent, both observable in the same place.

---

## Instrumentation

Hand-rolled OpenTelemetry spans, not auto-instrumentation. The gate reads spans
back *by attribute name*, so the names have to be exactly what the spec says
rather than whatever a library emits this month.

| Attribute | Source |
|---|---|
| `gen_ai.operation.name`, `gen_ai.provider.name` | GenAI semconv |
| `gen_ai.request.model`, `gen_ai.response.model` | GenAI semconv |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | GenAI semconv |
| `gen_ai.tool.name`, `gen_ai.tool.call.id` | GenAI semconv |
| `eval.run_id`, `eval.case_id`, `vcs.commit_sha` | Preflight |
| `preflight.cost_usd` | Computed from the price table in `preflight.yaml` |

> Every `gen_ai.*` attribute above is at **Development** stability in the
> OpenTelemetry spec. Names may still churn between releases; if a query stops
> matching, check the spec version first.

Cost is computed inside the span helper on exit, from the committed price table —
so `preflight.cost_usd` and the token counts can never disagree.

---

## Two caveats, stated plainly

**Deep links in the GitHub PR comment don't resolve after the job ends.** CI
stands up its own SigNoz inside the runner and tears it down on completion, so
those trace IDs stop existing. The links are correct, the data is gone. Set
`PREFLIGHT_SIGNOZ_PUBLIC_URL` to point CI at a persistent SigNoz and they
survive; or run `make ci-local`, which produces the same comment against your
local stack where every link opens the waterfall.

**The diagnosis agent is strong on *what*, weaker on *why*.** It reliably names
the worst case, quotes the deltas correctly, and cites span-level evidence it
could only have obtained through MCP. On root cause it hedges across a few
plausible mechanisms where the truth is a single prompt edit. A tightened prompt
that demanded a concrete cause reasoned better and investigated *worse* — it
stopped grouping by tool name and asserted something false. We kept the honest
hedge. The A/B is recorded next to the prompt so nobody "improves" it back.

---

## What we learned

**An unqualified attribute name in a SigNoz filter resolves to the *resource*
attribute, not the span attribute.** `vcs.commit_sha = 'abc'` matched 0 spans
while `attribute.vcs.commit_sha = 'abc'` matched 42. In ordinary CI — one
process, one commit — the two values agree and the bug is invisible. `groupBy` is
immune because it takes an explicit `fieldContext`, which is exactly why the
first probe looked fine.

**A collector that reports healthy is not a pipeline that persists data.** The
first real CI run failed with `0/32` spans while every health check stayed green.
SigNoz's collector fetches its config over opamp shortly after boot and
*restarts* — everything exported during that window is accepted over OTLP and
then dropped. The fix isn't a longer timeout; it's gating on the property you
actually depend on. `scripts/wait_for_pipeline.py` writes a probe span and polls
until it reads back.

**An acceptance check for a model's output must assert something the model could
not have guessed.** "Does the explanation name the worst case?" is nearly
worthless when the gate's own table is in the prompt — a model that called
nothing could pass by copying its input. Find a fact that lives only on the far
side of the tool call, and assert on that. Everything else measures fluency.

**Fail loudly on partial ingest rather than diff a half-written run.** CI writes
traces and immediately reads them back, with an exporter flush, collector
batching and a ClickHouse insert in between. Reading too early yields a silently
wrong diff, which is the worst failure mode here because nothing looks broken.
The poller earned its place before it ever ran in CI: it caught a bug in our own
response parser by reporting `0/6` instead of quietly returning an empty list
that downstream code would have read as "no regression."

More in [`.plans/learnings.md`](.plans/learnings.md) and [`DECISIONS.md`](DECISIONS.md).

---

## Layout

```
casting.yaml / casting.yaml.lock   Foundry — judges re-run this
preflight.yaml                     thresholds + model price table
agent/reference.py                 the agent under test
preflight/instrument.py            llm_span() / tool_span() / retrieval_span()
preflight/query.py                 SigNoz /api/v5/query_range client
preflight/contracts.py             RunSummary → DiffReport, the pinned seam
preflight/differ.py                the gate
preflight/report.py                PR comment + SigNoz deep links
preflight/diagnose.py              the diagnosis agent
preflight/mcp.py                   SigNoz MCP client
preflight/budget.py                hard spend cap, fails closed
suite/cases/*.yaml                 the golden suite
dashboards/ · alerts/              applied through MCP
.github/workflows/preflight.yml    the gate, in CI
```

---

## AI assistance

Built with **Claude Code** (Claude Opus 5) as a coding assistant, disclosed per
Agency Protocol 7. Planning was written before kickoff per Protocol 8; see
[`BUILD_PLAN.md`](BUILD_PLAN.md), which was not edited after work began —
deviations from it are logged in [`DECISIONS.md`](DECISIONS.md) instead.
