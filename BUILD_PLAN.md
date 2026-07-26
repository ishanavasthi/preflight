# Build Plan: Preflight

**Repo:** https://github.com/ishanavasthi/preflight (exists, empty — verified)
**Track:** 1 — AI & Agent Observability
**Team:** solo (ishanavasthi)
**Written:** 2026-07-27, pre-kickoff. Per Agency Protocol 8, this is a written plan; code starts at kickoff.

---

## Problem

A team ships an AI agent behind a PR workflow. Someone edits one line of a prompt, or swaps a tool
description, or bumps a model. The unit tests still pass — the code didn't change shape. What did
change is invisible: the agent now makes two extra retrieval hops, reaches for a different tool, and
costs 3× per task. Nobody sees it at review time. They see it in next month's invoice, or in a
latency alert at 2am, or in a user complaint. **Solved means: the regression is caught in CI, on the
PR that caused it, with the specific span that explains it one click away.**

## Done when (v1, observable outcomes)

- A PR that degrades the agent fails CI, and the PR comment names the metric, the delta, and links
  to the exact LLM span in SigNoz.
- A PR that doesn't degrade it passes, with the same numbers shown as a green diff.
- A judge clones the repo, runs `foundryctl cast -f casting.yaml`, and reproduces the whole stack —
  SigNoz, MCP server, dashboards, alerts — without asking a question.
- Every number in the gate came out of SigNoz's query API, not a local JSON file.

---

## Architecture in one paragraph

Python. The reference agent is instrumented with the OpenTelemetry SDK — hand-rolled spans following
the GenAI semantic conventions, so attribute names are exactly what the spec says rather than
whatever an auto-instrumentation library emits. Every run of the golden suite emits one trace per
case, tagged `eval.run_id` / `eval.case_id` / `vcs.commit_sha`, plus run-level metrics on the same
dimensions. The differ then queries **SigNoz** for per-run aggregates on the candidate SHA and the
baseline SHA and compares them. SigNoz is the datastore, not a dashboard bolted on the side — there
is no local results file. That's the design bet, and it's also the whole "Best Use of SigNoz" pitch.

**Novelty budget: spent on nothing.** OTel SDK, OTLP over HTTP, a REST client, arithmetic, a GitHub
Action. Every risk in this project is integration risk, not technology risk — which is the right
shape for one person and one night.

### Layout

```
preflight/
├─ casting.yaml / casting.yaml.lock   # Foundry — judges re-run this
├─ preflight.yaml                     # thresholds + model price table
├─ agent/                             # the reference agent under test
├─ preflight/
│  ├─ otel.py          # tracer + meter + OTLP exporter setup
│  ├─ instrument.py    # llm_span() / tool_span() / retrieval_span()
│  ├─ runner.py        # golden-suite harness
│  ├─ query.py         # SigNoz /api/v5/query_range client
│  ├─ differ.py        # baseline vs candidate, thresholds
│  ├─ report.py        # markdown report + SigNoz deep links
│  ├─ diagnose.py      # diagnosis agent over SigNoz MCP
│  └─ cli.py           # preflight run | diff | explain
├─ suite/cases/*.yaml
├─ dashboards/*.json  ·  alerts/*.json
└─ .github/workflows/preflight.yml
```

---

## Unknowns

### Design-changing — resolved by doc spike before writing code

**1. Can the SigNoz query API aggregate and group by a custom span attribute?**
This is the load-bearing question: if the answer is no, SigNoz can't be the source of truth for the
gate and the whole premise changes.

> **Verified.** `POST /api/v5/query_range`, auth header `SIGNOZ-API-KEY: {key}`. Body carries
> `start` / `end` (epoch ms), `requestType` (`time_series` | `scalar` | `raw` | `trace`), and
> `compositeQuery.queries[]` of `type: "builder_query"`. The spec takes `aggregations[]`
> (`count()`, `sum(f)`, `avg(f)`, `p95(f)`, …), `filter.expression` (operators `=`, `!=`, `>`, `IN`,
> `CONTAINS`, `REGEXP`, `EXISTS`, …), `groupBy[]` (each `{name, fieldContext}` where `fieldContext`
> is `attribute` / `resource` / `span`), `order[]`, and `having`.
>
> **Likely** — grouping by `eval.run_id` with `fieldContext: "attribute"` works exactly as the
> documented `service.name` / `resource` example does; the shape is documented but I have not
> executed it. If wrong: fall back to one query per run with `filter.expression` pinning
> `eval.run_id = '<id>'` and no `groupBy` — more round trips, same result, no design change.

**2. How long after a run can CI query the traces?**
CI writes traces and immediately wants to read aggregates. Exporter flush + collector batching +
ClickHouse ingest all sit in between.

> **Assumption: end-to-end lag is seconds, not minutes.** If wrong: the differ blocks CI or reads a
> half-ingested run and reports garbage — the worst failure mode in the project, because it's
> silent. **Mitigation is not optional and ships in M1:** after `force_flush()`, poll
> `count()` filtered on `eval.run_id` until it equals the expected span count or a 120s timeout
> expires, and *fail loudly* on timeout rather than diffing a partial run.

**3. Does Foundry produce a lock file and install the MCP server in one step?**
Field Requirement 3 makes this a submission gate, not a nicety.

> **Verified.** Install: `curl -fsSL https://signoz.io/foundry.sh | bash`. A minimal `casting.yaml`
> is `apiVersion: v1alpha1` / `kind: Installation` / `metadata.name` / `spec.deployment: {flavor:
> compose, mode: docker}`. `foundryctl cast -f casting.yaml` runs all three stages (validate →
> generate → start) and produces `casting.yaml.lock` plus compose files under `pours/deployment/`;
> `foundryctl gauge` / `forge` / `docker compose up -d` is the same thing split apart. The MCP
> server is **off by default** — set `spec.mcp.spec.enabled: true`, it listens on port `8000`.

### Detail-level — resolve when reached, don't spike

- The SigNoz trace deep-link URL format. **Get it by opening one trace in the UI and copying the
  address bar** — do not guess it into `report.py`, a broken link in the PR comment is exactly the
  kind of thing a judge clicks.
- Dashboard-import and alert-rule API payload shapes (see M5 — there's a shortcut).
- Exact OTLP endpoint/port the Foundry compose deployment exposes.

---

## Milestones

Riskiest assumption first. **Run each check the moment the milestone ends. A failed check blocks the
next milestone** — do not batch verification to the morning.

### M1 — Walking skeleton (~90 min) ⚠️ highest risk

Foundry up with MCP enabled. One trivially instrumented agent call emitting one trace tagged
`eval.run_id`. `query.py` reads it back through `/api/v5/query_range`. The ingest-lag poller exists
in this milestone, not later.

**Check:** `preflight run --cases 1 && preflight query --run-id <id>` prints a non-zero span count
that came from the HTTP API — and the run ID it prints was generated less than 60 seconds earlier.

> If this check fails, **stop and read the kill criteria.** Everything downstream assumes it.

### M2 — Golden suite + full instrumentation (~90 min)

6–8 cases in `suite/cases/`. The reference agent is non-trivial: multi-tool, one retrieval hop, 5–8
spans per run. Spans carry the verified GenAI attributes — `gen_ai.operation.name`,
`gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.tool.name`, `gen_ai.tool.call.id`
— plus `eval.run_id`, `eval.case_id`, `vcs.commit_sha`, and `preflight.cost_usd` computed from the
price table. Run-level metrics emitted on the same dimensions. Logs carry trace context.

> Note: every GenAI attribute above is **Development** stability in the spec. Say so in the README —
> it reads as rigor, and it inoculates you if a judge notices churn.

**Check:** run the suite twice under two different fake SHAs; one query grouped by `vcs.commit_sha`
returns two rows with plausible, different token totals.

### M3 — Differ + gate (~90 min)

`preflight diff --baseline <sha> --candidate <sha>` pulls both runs from SigNoz, computes deltas on
cost/task, tokens/task, p95 latency, tool-call trajectory divergence, retrieval hops, and success
rate, applies thresholds from `preflight.yaml`, prints a markdown table, exits non-zero on breach.

**Check:** on the seeded regression branch → exit 1 with the cost delta named; on baseline → exit 0.
**Seed the regression branch now, before you need it** — a one-line prompt edit (e.g. appending
"enumerate every option before answering") that reliably inflates tokens and adds a hop.

### M4 — GitHub Action + PR comment (~60 min) — **demo complete here**

Workflow runs the suite on PR, resolves the baseline SHA from the merge base, runs the differ, posts
a comment with the delta table and per-case deep links, fails the check.

**Check:** open a real PR from the regression branch on
`github.com/ishanavasthi/preflight`. CI goes red, the comment renders, and a link in it opens the
offending span in SigNoz. **Watch this happen once, end to end — this is the 90 seconds of video.**

> **Stop here and assess the clock.** If it is past ~06:00, freeze scope and go straight to M7. A
> submitted project with M1–M4 beats an unsubmitted one with M6.

### M5 — Dashboards + alerts as code (~45 min)

Committed dashboard JSON (agent health, cost-per-task by commit, tool trajectory breakdown, per-case
latency) and at least two real alert rules (cost-per-task burn rate, tool-error rate), applied by
`make signoz-apply`.

> **Shortcut worth taking:** the SigNoz MCP server exposes 40+ tools including full dashboard CRUD
> with template import, and alert-rule create/update/delete (verified). Build these *through MCP*
> from Claude Code rather than reverse-engineering REST payloads. It's faster, it removes an
> unverified unknown, and it is itself MCP usage you can point at in the submission.

**Check:** delete a dashboard from the UI, run `make signoz-apply`, it comes back identical.

### M6 — Diagnosis agent over MCP (~60 min)

An agent that, given a failed gate, queries the SigNoz MCP server to explain *why* in plain English,
and appends that to the PR comment. It is itself instrumented into SigNoz.

> Self-hosted MCP config is **verified**: stdio (recommended) or HTTP transport, env `SIGNOZ_URL` +
> `SIGNOZ_API_KEY`, admin role required to mint the key.

**Check:** force a failure; the explanation names the specific case and metric, and the agent's own
investigation shows up as a trace in SigNoz. That full-circle screenshot is the strongest single
image in the blog post.

### M7 — Ship (~2 hrs, non-negotiable)

README opening with the problem, not the architecture. ≤3-min video (problem → architecture → the
M4 demo → what you learned). New blog post. Submit the form.

**Check:** every claim in the "how I used SigNoz" answer is true of what's actually in the repo.
Delete any bullet you didn't reach — judges re-run Foundry and look.

---

## Model choices

| Role | Recommendation | Rationale |
|---|---|---|
| Reference agent (runs 8× per CI run, repeatedly) | `claude-sonnet-5` | $3/$15 per MTok, currently **$2/$10 introductory through 2026-08-31**. Fast suite, cheap iteration, and cost deltas still show clearly. |
| Diagnosis agent (runs once, on failure, reads MCP output) | `claude-opus-5` | $5/$25 per MTok. This is the one that has to reason well over trace data; it runs rarely. |

Your call — I picked for iteration speed on a night with a fixed budget, not because Sonnet is the
"right" tier. If you'd rather demo Opus end-to-end, swap the reference agent and cut the suite to 4
cases. Put both IDs and prices in `preflight.yaml` so the cost math is auditable in the repo.

---

## Cut list (v1 explicitly does NOT do)

- **Chaos / fault injection** — re-enter only if M6 lands before 07:00.
- **Multi-language agent support** — Python only, permanently for v1.
- **A web UI** — the PR comment and the SigNoz dashboards are the interface. Excluded permanently;
  building one would be inverted effort while the gate is the product.
- **Auth, multi-tenancy, config UI** — you are the only user. Excluded permanently.
- **Historical trend backfill / statistical significance testing** — thresholds are fixed numbers in
  YAML. Re-enter when >1 user asks.
- **Hosted deployment** — "Deployed link" is optional on the form. Leave it blank or link a
  dashboard screenshot.

Changing this list mid-build is a logged decision in `DECISIONS.md`, not silent drift.

---

## Kill criteria

- **If M1's check fails after 2 hours** — SigNoz can't serve per-run aggregates the way the design
  needs — then the SigNoz-as-source-of-truth design dies. Fall back: differ reads a local artifact
  for the gate, SigNoz keeps traces/dashboards/alerts for the *diagnosis* half. Weaker submission,
  still a submission. Do not keep debugging past the timebox.
- **If M3 takes more than 2× its estimate**, cut the trajectory-divergence and retrieval-hop metrics
  and gate on cost + latency + success rate only. Three metrics that work beat six that don't.
- **If it is past 06:00 and M4 is not green**, stop building entirely and start M7. The video, blog,
  and form are hard requirements; M5 and M6 are score multipliers on a submission that must exist
  first.

---

## Risks

- **Ingest lag makes the gate flaky** — consequence: CI fails randomly and a judge sees a red build
  for the wrong reason. Trigger: any run where the poller times out. Act by raising the timeout and
  saying so in the README rather than shortening it to look fast.
- **Golden-suite non-determinism** — the agent varies run to run, so a "regression" may be noise.
  Consequence: the demo doesn't reproduce on stage. Trigger: two baseline runs differing by more
  than your threshold. Act by setting thresholds well above observed run-to-run variance and by
  making the seeded regression large (3×, not 15%).
- **Foundry install eats an hour** — consequence: M1 slips and everything compounds. Trigger: not
  green 45 minutes in. Act by dropping to plain `docker compose` for the build and returning to
  Foundry before submission (`casting.yaml` + `.lock` in the repo is mandatory, but it does not have
  to be what you developed against).
- **Undisclosed AI assistance = disqualification.** Agency Protocol 7 permits AI assistants but
  requires declaring them. You are building this with Claude Code. Put one line in the README and
  the submission form. Trigger: none — just do it, it costs nothing and the downside is total.

---

## Tonight's order

`M1 → M2 → M3 → M4 → (assess clock) → M5 → M6 → M7`

Create the repo skeleton and push an empty commit first, so the GitHub URL on the form is live and
CI has something to attach to.
