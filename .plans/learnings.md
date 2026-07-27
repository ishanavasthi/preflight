# Learnings

Newest first.

---

## 2026-07-27 — M6: the diagnosis agent, and what it costs to make a model investigate

**What changed.** `preflight/diagnose.py` takes a failed gate and explains it in
plain English, driving the **SigNoz MCP server** as its only source of facts —
and its own investigation lands in SigNoz as a 22-span trace. `preflight/mcp.py`
is the client, which is M5's transport *moved* rather than rewritten.
`make m6-check` is the acceptance check and passes with **no API key at all**.

**The check, run:**

        [1/4] gate the seeded regression (e0592cf8 -> 59607e52)
          PASS  the gate failed, so there is something to explain
        [2/4] diagnosis agent, driving the SigNoz MCP server
               6 turn(s), 15 MCP call(s), 49414+2270 tokens, $0.06076
        [3/4] PASS names the worst case (damaged-item)
              PASS names the regressed metric  --  cost, token
              PASS cites span-level evidence absent from its own prompt
                                               --  policy_search, policy-kb
              PASS the investigation actually went through MCP -- 15 tool call(s)
        [4/4] 22 span(s): diagnose regression, chat claude-haiku-4-5-20251001,
              execute_tool signoz_aggregate_traces, execute_tool signoz_get_trace_details
        PASS  diagnosis trace: c4696bb3c183db783f8260f748eb21e9

**The headline finding: an acceptance check for a model's output has to assert
something the model could not have guessed.** The obvious test — "does the
explanation name the worst case?" — is nearly worthless here, because the gate's
own per-case table is in the agent's prompt. A model that read nothing and
called nothing could pass it by copying its input. What makes the check real is
that `policy_search` and `policy-kb` appear **nowhere in the prompt**: they exist
only as span attributes, so quoting them is proof the agent went to SigNoz and
came back. The general rule: when testing a generated explanation, find a fact
that lives only on the far side of the tool call, and assert on that. Everything
else measures fluency.

**Steering the conclusion cost me the evidence.** The first prompt produced a
diagnosis that found `policy_search` and `policy-kb` but hedged the root cause
across three possibilities. So I tightened it — demand both-sides measurement,
name a concrete cause from a fixed list, say why the others don't fit. The
second run reasoned *visibly better*: it correctly identified a system-prompt
edit and explicitly ruled out a model swap on the evidence that every span still
showed `claude-haiku-4-5-20251001`. It also **never grouped by
`gen_ai.tool.name`**, and therefore asserted "the same tools appear in both
runs" — which is flatly false; `policy_search` is new in the candidate. The
check caught it. Same model, same tools, same data, $0.048: the extra
instruction about *what to conclude* pulled effort away from *gathering what the
conclusion rests on*. I kept the first prompt. A confidently wrong diagnosis is
worse than a correctly hedged one, and the A/B is recorded next to the prompt so
the next person doesn't "improve" it back.

**A pinned time window is what makes a recorded investigation replayable.** The
cassette key is a hash of the whole request, and every MCP call carries `start`
and `end`, so the window is *inside* the key. An unpinned window means every
cassette misses ten minutes after recording, and a "reproducible" check quietly
becomes a $0.06 API call. Two fixes, both needed: round `end` up to a 10-minute
bucket so two runs a minute apart ask identical questions, and let it be pinned
outright (`--window-end`) so the committed cassettes name the slice of time they
were recorded against. With both, `make m6-check` replays byte-identically with
`ANTHROPIC_API_KEY` unset — which is the state a judge cloning this repo is in.
The general shape: **anything that varies between runs and reaches the request
is part of your cache key, whether you meant it to be or not.**

**Compaction is a cost control, not a nicety.** A `signoz_get_trace_details`
response is 17.5k characters, of which the envelope, per-column metadata and ~30
always-null well-known fields (`k8s.*`, `db.*`, `cloud.*`) are almost all of it.
`mcp.compact()` gets it to 5.1k. That matters more than it looks: in a tool-use
loop the whole transcript is re-sent every turn, so one fat tool result is not
paid once, it is paid on every subsequent turn. The same logic applies to the
tool schemas — the server's advertised `inputSchema`s are faithful and verbose,
and pasting all 42 would cost >10k input tokens *per turn*. `anthropic_tools()`
reads them live but keeps 2 tools and only the parameters the agent may set.
Curation is ours; the types stay the server's, so a schema that changes upstream
is a loud `KeyError` at startup rather than a 400 mid-investigation.

**Gotcha: the diagnosis nearly poisoned the gate.** The instinct is to stamp
diagnosis spans with the SHA they are about. That would have been a real
outage-in-waiting: `differ.resolve_run()` resolves a SHA to its *most recent*
`eval.run_id`, and a diagnosis emitted after the candidate suite wins that race
— so the next gate run would diff the agent against its own explanation.
Diagnosis spans therefore carry `vcs.commit_sha = diagnosis-<sha>` under service
name `preflight-diagnose`. Same family as M5's `name`-is-not-an-idempotency-key
trap: a field that looks like a correlation key is a *selector* somewhere else.

**Gotcha: poll for the root span, not for any span.** The first check run failed
[4/4] reporting the root `diagnose regression` span missing — it was there 200ms
later. The root closes and exports *last*, so "the trace exists" goes true
before the trace is *complete*, and a poll that breaks on the first non-empty
read observes a torn trace. Wait on the last-written span. This is M1's
ingest-lag lesson at one span's resolution rather than one run's.

**Honest read on the diagnosis quality.** Strong on *what*: it names
`damaged-item`, quotes +284% cost / +253% tokens / +292% latency correctly, and
finds the two structural facts that only exist in spans (a new `policy_search`
tool, `policy-kb` hops 6 → 14). Weaker on *why*: it says "an additional agentic
loop or reasoning step... a retry mechanism, a multi-turn reasoning pattern, or
changed decision logic" where the truth is a system-prompt edit mandating a
fixed four-step tool sequence — directionally right, three-ways hedged, and it
never names a prompt change. It also slips once: "`policy_search` 8 times
instead of 6" is wrong (the baseline is **0** — it appears to have read across
from `lookup_order`'s 6). A reviewer handed this would look in the right place
and would not be misled about severity, but the specific root-cause sentence is
not something to quote without checking.

**Cost: $0.1093** — two live investigations at ~$0.06 and ~$0.048, plus a free
schema validation through `/v1/messages/count_tokens`, which accepts `tools` and
costs nothing (worth knowing: it is the only way to prove a tool schema is
well-formed without paying for a completion). Everything else — the whole loop,
the instrumentation, the full-circle proof — was developed against a **stubbed
model response**, which cost $0 and caught every plumbing bug before a single
paid call. Total project spend $0.2216 of $1.00.

**Aside: the API key was dead for ~25 minutes mid-milestone**, returning `401
invalid x-api-key` (not `credit_balance_too_low` — worth distinguishing, they
mean opposite things). It recovered on its own. The useful response was to keep
building everything that needed no inference and prove the full-circle property
with a stub, so that when the key returned the milestone was one command from
done.

---

## 2026-07-27 — M5: dashboards and alerts as code, through MCP; and the metrics read path cracked

**What changed.** Four dashboards (24 panels) and two alert rules, committed as
JSON under `dashboards/` and `alerts/`, reconciled onto SigNoz by
`make signoz-apply`. Everything goes through the **SigNoz MCP server** — 42
tools, full dashboard and alert CRUD — so BUILD_PLAN's M5 shortcut is a true
submission claim and not an aspiration. `make signoz-verify-panels` executes
every committed panel query and reports 27/27 returning real data;
`make signoz-apply-check` is the acceptance check, automated.

**The M5 check, run:**

        [2/5] delete preflight-tool-trajectory (id=019fa222-0326-...)
               confirmed gone -- 3/4 preflight dashboards remain
        [3/5] make signoz-apply (re-apply from the committed JSON)
               tool-trajectory.json: created  019fa225-616b-...
        [4/5] diff the restored definition against the snapshot
               identical: 7902 bytes of definition match exactly
        [5/5] idempotency: apply again, assert nothing is created
               converged -- every dashboard updated in place, none created
        PASS: the committed JSON is the source of truth for SigNoz

**The headline finding: M2's "metrics don't read back" was a metric-type
auto-detection problem, not a shape problem.** M2 timeboxed this after trying the
base name, `.sum` and `.bucket`, with and without `temporality: Cumulative`, and
several `spaceAggregation` values — all zero rows, with the data plainly visible
in ClickHouse. The missing move was to stop letting SigNoz infer the type.
SigNoz catalogues `preflight.case.*` as `Histogram / Cumulative / isMonotonic`,
and a query that auto-fetches that metadata takes a histogram-percentile path
that returns **zero rows while still scanning 48,527 of them** — the scan count
is the tell, and it is the thing to look at when a query is empty but not fast.
The recipe that works:

        metricName:       preflight.case.cost_usd.sum   # the suffixed series, not the base
        metricType:       gauge                          # explicit; do NOT let it auto-fetch
        timeAggregation:  max                            # explicit
        spaceAggregation: sum

Cross-checked against the trace-derived total for the same commit: **$0.0769
both ways**, exactly. Two transferable lessons. First, `rowsScanned > 0` with an
empty result set means the filter matched and the *aggregation* dropped
everything — a completely different bug from `rowsScanned == 0`, and the two are
indistinguishable if you only look at the row count. Second, a backend that
helpfully auto-detects schema will auto-detect you into a dead end; when a query
returns nothing, pin every inferred parameter explicitly before concluding the
data is unreachable.

**And then the dashboards read traces anyway.** Cracking it did not change the
design, which is worth being honest about. The gate reads traces
(`run_summary_typed`), so a trace-backed dashboard cannot disagree with the PR
comment, while a metrics-backed one can — and the day those two numbers diverge
in front of a reviewer, the gate is finished. Trace aggregation also has no
temporality or bucket-alignment subtleties, and I had verified the metrics path
for one query, not for twenty-four panels. Solving a blocker and then not
depending on it is a legitimate outcome; the finding is in DECISIONS.md so
nobody claims "metrics dashboards" on the strength of it.

**Gotcha: a `name` that looks exactly like an idempotency key, and isn't.**
SigNoz dashboards carry a top-level `name` slug. Posting the identical payload
twice creates **two dashboards** with the same `name` and different UUIDs. So
`make signoz-apply` reconciles rather than upserts: list, match on `name`, update
in place by `id`, and delete stale duplicates so the deployment converges on git.
The same shape applies to alert rules, matched on their `alert` title.

**Gotcha inside that one, and it fails silently:** `signoz_list_alert_rules`
returns the rule UUID as **`ruleId`**, while `signoz_create_alert` returns it as
`id` and `signoz_update_alert` expects `id`. Reading `id` off a listing yields
`None`; the update then targets nothing and still reports success. Caught only
because a debug print showed `id=None` next to a rule that was demonstrably
firing. Same family as M1's stale-auth trap: the call succeeded, so nothing drew
attention to it.

**Four payload shapes the MCP tool schema does not describe.** The published
`inputSchema` is a faithful rendering of the Go types and still leaves these to
be discovered by 400. Probing for them took four cycles and no reading would
have shortened it:

1. `schemaVersion` must be `"v6"` — typed as a bare string with no enum.
2. The grid is **12 columns wide in total**, not 24, so a half-width panel is
   `width: 6`.
3. **Table panels format per column** (`columnUnits`, alias → unit) and reject a
   bare `unit` with *"json: unknown field"*. Every other panel type takes `unit`.
4. **A panel accepts exactly one query entry.** Multi-query maths goes inside a
   single `signoz/CompositeQuery` plugin carrying the input `builder_query`
   specs plus a `builder_formula` — not three sibling entries, which fails with
   *"panel must have one query"*.

**Takeaway: verify a dashboard the way you verify a link.** M4's lesson was that
a SigNoz 200 proves nothing because the SPA answers 200 to everything, so you
ask the API the page calls to render itself. Dashboards have the identical
failure mode one level up: a panel with a subtly wrong filter renders perfectly
and is simply empty, indistinguishable from a healthy panel on a quiet system.
`make signoz-verify-panels` therefore re-executes every committed panel query
through `/api/v5/query_range` — **using each panel's own declared request type**,
because scalar and time_series return different shapes (`results[].data` versus
`results[].aggregations[].series[]`) and counting the wrong one reads as zero.
Panels that are *legitimately* empty are listed in an `ALLOW_EMPTY` table with a
written reason, so "no rows" stays a hard failure everywhere else. That check is
what found the one real defect in the set.

**Thresholds measured, not guessed — same as M3.** Baseline cost per task is
$0.00255 and the seeded regression $0.00550, so the burn-rate ceiling sits at
$0.004: it fires on the regression and stays quiet on `main`. Verified live —
the cost rule sat in `firing` and the tool-error rule in `inactive` at the same
moment. The tool-error rule being quiet is the correct outcome, not a gap: no
tool span has ever errored here, and expressing it as a rate rather than a count
is what keeps one flaky call from paging anybody.

**Cost: $0.00.** Nothing in M5 calls a model. The two suite runs that produced
the dashboard data ran under `PREFLIGHT_REPLAY=1` off committed cassettes, which
is the whole point of M2's cassette design paying rent three milestones later.
Total project spend is still $0.1123 of $1.00.

---

## 2026-07-27 — M2/M3/M4 integrated: three milestones built in parallel, verified together

**What changed.** M2, M3, and M4 were built concurrently by three agents in one
working tree rather than in sequence, then verified as a whole. Everything
composes: `make ci-local BRANCH=seeded-regression` runs the real Action logic
end to end — two git worktrees at the merge base and the branch head, the suite
against each, then the gate — and exits non-zero with a rendered comment whose
six SigNoz deep links all resolve (`route=200 waterfall=200`).

**Parallelism worked because the seams were pinned first, not because the
milestones were independent.** They aren't: M3 reads what M2 writes, M4 renders
what M3 produces. What made concurrency safe was committing `contracts.py`
(`RunSummary` → `DiffReport`) and a `report.py` stub with M4's real signatures
*before* dispatching, plus explicit per-agent file ownership. Putting
`RunSummary.metric()` in contracts rather than in the differ mattered most — the
gate and the renderer cannot disagree about what "cost per task" means, because
there is exactly one implementation.

**The budget mechanism earned its keep.** A shared $1 of API credit across three
agents is precisely where a "please be careful" instruction fails. A file-locked
ledger that fails closed, plus cassette replay, meant only M2 ever spent: **$0.11
of $1.00 across 69 calls**, with M3's 24-assertion suite and M4's full CI
rehearsal both running at zero cost. Replay also made the demo reproducible,
which retires the golden-suite non-determinism risk as a side effect.

**Two operational gotchas that look like bugs and aren't:**

- `make ci-local` returns **2**, not 1, when the gate breaches. GNU make exits 2
  whenever a recipe fails, so the gate's own exit 1 is not propagated. The
  workflow calls `preflight diff` directly and is unaffected — but anyone
  reading `make`'s exit code as the verdict will misread it.
- `preflight diff` with the same SHA on both sides exits **3**, not 0. That is a
  deliberate guard: comparing a commit to itself is *cannot evaluate*, not *no
  regression*, and the workflow treats exit 3 as a distinct failure from exit 1.
  In real CI the merge base is never the PR head, so it never fires.

**Takeaway.** The distinction that made this work is between *code* dependencies
and *interface* dependencies. M2→M3→M4 is a hard code dependency, which is why
sequential was the right call when nothing was pinned. Spending twenty minutes
writing the interfaces down converted it into three independent problems. The
cost of getting that wrong is high (three agents coding against a moving target),
but the check is cheap: can each agent's output be described as a dataclass the
next one imports? If yes, parallelise; if no, don't.

---

## 2026-07-27 — M2 golden suite: real agent, cassette replay, metrics, logs

**What changed.** `agent/reference.py` is now a real tool-calling loop against
`claude-haiku-4-5-20251001` over an in-repo fake dataset (`agent/data.py`):
one grounding retrieval hop, then up to five model turns with three tools
(`lookup_order` / `policy_search` / `check_inventory`). Six cases in
`suite/cases/`, 5–13 spans each. Spans carry the full GenAI semconv set plus
`preflight.cost_usd` from the price table; run-level metrics are emitted on
`eval.run_id` / `eval.case_id` / `vcs.commit_sha` under the names pinned in
`contracts.py`; logs export over OTLP with trace context attached.
`query.run_summary_typed()` returns `contracts.RunSummary` — the M3 seam.
Branch `seeded-regression` holds a one-line prompt edit worth +150% cost.

**The whole thing runs on a record/replay cache.** `preflight/replay.py` keys
each request by a hash of (model, system, messages, tools, max_tokens,
temperature) and stores the response as JSON under `.cassettes/`, which is
committed on purpose. `PREFLIGHT_REPLAY=1` — or simply no API key — replays and
never calls out, so a fresh clone runs the full suite offline and for free. Two
problems, one fix: the project has $1 of credit, and a gate whose numbers move
between runs is not a gate.

**Takeaway: a test double has to be faithful in whatever dimension you gate on,
not just the obvious one.** The first cassette design replayed tokens and
content perfectly and dropped latency on the floor — replayed cases finished in
~0.2ms. Tokens and cost were exact, so the cache looked correct, and
`p95_latency_ms` had quietly become a random number generator: ordinary
0.2ms/0.5ms jitter is a +150% swing that trips any threshold anyone would pick.
That is the "gate is flaky, so the team switches it off" failure in the risk
register, arriving through the component built to make the gate *stable*. Fix
was to record the provider's wall time in the cassette and sleep it on replay;
p95 is now 3.19s baseline vs 8.54s regression — a real, reproducible signal.
M3 independently hit the same zero-latency problem from the other side and added
an absolute noise floor; both fixes are worth keeping.

**Gotcha: the harness ceiling silently caps the signal it is supposed to
measure.** With `MAX_MODEL_TURNS = 3`, the seeded regression came out at +16%
tokens — precisely the "15%, not 3×" case BUILD_PLAN warns against — and the
obvious read was "the prompt edit is too weak." It wasn't: the *cap* was
truncating the trajectory, so a longer-running agent was being clipped back into
looking like baseline. Raising the ceiling to 5 changed nothing for baseline
(every case still terminates on `end_turn` in two calls, all twelve cassettes
still hit) and let the regression show +130%. Check the instrument before
concluding the effect is small.

**Gotcha, and the most portable finding here: what inflates agent cost is
serialisation, not verbosity.** Two attempts at the seeded regression asked the
model to be more thorough — "enumerate every option", "check every relevant
tool, one per step". Both landed at +16–18% tokens, because the model batches
its tool calls into a single parallel turn: more tools, same number of turns,
input context re-sent the same number of times. What actually moved the number
was a **dependency chain** — call `policy_search`, then `lookup_order`, then
`check_inventory` *for the SKU you found on that order* — where each tool's
input requires the previous tool's output. That forces genuinely sequential
turns, and each turn re-sends a growing transcript: +130% tokens, +133%
retrieval hops, +150% cost. Worth remembering when reasoning about agent spend:
turn count is the multiplier, output length is rounding error.

**Gotcha: SigNoz explodes OTel histograms into suffixed series, and only
`.bucket` is registered as queryable.** The six `preflight.case.*` histograms
land in ClickHouse as `.bucket` / `.count` / `.sum` / `.min` / `.max`, with
correct `eval.run_id` / `eval.case_id` / `vcs.commit_sha` labels — but
`signoz_metrics.distributed_metadata` records only `.bucket` with
`type = Histogram`. Every `signal: "metrics"` query I tried through
`/api/v5/query_range` returned zero rows (base name, `.sum`, and `.bucket`;
with and without `temporality: Cumulative`; several `spaceAggregation` values).
The data is unambiguously there — verified in ClickHouse — so this is a read-path
shape I did not crack, not a missing emit. Left for M5, which BUILD_PLAN already
says to build through the SigNoz MCP server rather than by hand-rolling these
payloads. **Nothing depends on it today:** the gate reads traces via
`run_summary_typed`, which is fully verified end to end.

**`temperature=0` on a golden suite.** Not for determinism at inference — it
does not give that — but so that *re-recording* cassettes doesn't rewrite every
expected answer and force the six `expect_contains` assertions to be re-tuned.

**Process, learned the annoying way: `git add -A` in a shared working tree
commits your teammates' work in progress.** Three agents share this checkout;
my first M2 commit swept in M3's then-unfinished `differ.py`, `cli.py`, and
`scripts/m3_check.py`. Nothing was lost and their files imported cleanly, so
unwinding it would have risked more than leaving it — but the fix going forward
is to stage explicit paths. Same lesson for branches: the `seeded-regression`
work was done in a `git worktree` at /tmp rather than by checking the branch out
here, which would have yanked the tree out from under two other agents mid-edit.

**Spend: $0.1123 across 69 calls**, against a $1.00 cap and a $0.30 target —
`preflight/budget.py` gated every one. Recording the six-case suite once costs
$0.0151; the rest went on one probe, one re-record after adding latency and
`temperature`, and three attempts at the seeded regression. Every subsequent run
of the suite is free.

---

## 2026-07-27 — M4 landed: the PR comment, and the deep link that isn't a guess

**What changed.** `preflight/report.py` is the real renderer: pass/fail banner,
a headline naming the breached metrics worst-first, the delta table, a "biggest
mover" line pointing straight at the offending trace, and a collapsed per-case
breakdown where every row deep-links to its own trace in SigNoz.
`.github/workflows/preflight.yml` forges SigNoz in-job from the committed
`casting.yaml`, bootstraps credentials, resolves the baseline from
`git merge-base`, runs the suite on the merge base *in a detached worktree* and
on the PR head, gates, and upserts one sticky PR comment. `PREFLIGHT_REPLAY=1`
throughout, so CI costs nothing and is deterministic.
`scripts/report_sample.py` renders samples and verifies links; `make ci-local`
rehearses the whole Action on a laptop.

**The deep-link format, and how it was actually found.** The plan said to copy
it out of the address bar. The bundle is a better source, because it explains
*why* the URL is the shape it is — and the M1 precedent (stale auth docs
recovered from `/assets/index-*.js`) said to look there first. Four independent
confirmations, all against v0.134.0:

1. `ROUTES.TRACE_DETAIL: '/trace/:id'` — a single trace, not `/traces/`.
2. The trace-detail chunk (`TraceDetailsV3-*.js`) reads the selected span from
   the query string: `searchParams.get('spanId')`, and a present `spanId` means
   "expand the waterfall to this span".
3. The UI's own **Copy link** handler builds `` `${pathname}?${params}` `` with
   `spanId` set — so the template is literally what SigNoz puts on your
   clipboard.
4. `POST /api/v4/traces/{id}/waterfall` returns the span list for a real trace
   and `{"type":"not-found"}` for a bogus one.

        {base}/trace/{trace_id}                    # the trace
        {base}/trace/{trace_id}?spanId={span_id}   # that span, pre-selected

**Verifying a link means asking the API, not asking for a 200.** SigNoz is a
SPA behind a catch-all: *every* path returns HTTP 200 with the same HTML shell,
including `/trace/deadbeef` and `/trace/this-is-not-a-trace`. A status-code
check would have "verified" a format that was completely wrong — the same trap
that made the stale auth endpoints in M1 look like they worked. The real check
is the API the page calls to populate itself, which is why
`scripts/report_sample.py --verify` hits `/api/v4/traces/{id}/waterfall` and
asserts a non-empty span list. All 6 links in a real gate report pass it.

**A gate that can't run must not look like a gate that failed.** `preflight
run` exits 2 when ingest never settles and `diff` exits 3 when there's nothing
to compare; neither means the agent regressed. Both get their own annotation
and a comment that says *the comparison did not happen*. Every path that
produces no `report.md` synthesises one — silence in CI is exactly how a broken
gate goes unnoticed, which is the failure mode this project exists to prevent.

**Gotcha: GitHub's default shell is `bash -eo pipefail`, so `code=${PIPESTATUS[0]}`
never runs.** The step dies on the failing command before it can capture the
exit code it exists to capture — and the gate's whole design is "post the
comment, *then* fail". Every step that inspects an exit code says `set +e`
first. Caught by extracting each `run:` block out of the YAML and executing it
under `bash --noprofile --norc -eo pipefail`, which is worth doing for any
workflow you can't afford to debug by pushing commits.

**Takeaway.** When the docs and the address bar are both weak sources, the
frontend bundle is the strongest one available — it gives you the format *and*
the reason. And when the server answers 200 to everything, "it returned 200" is
not verification; find the request the page makes to render itself and check
that instead.

---

## 2026-07-27 — M3 differ + gate landed; the gate fires and stays quiet

**What changed.** `preflight/differ.py` resolves a commit SHA to its most recent
`eval.run_id` in SigNoz, pulls both runs, computes all six metrics in
`contracts.GATED_METRICS` through `RunSummary.metric()`, applies the thresholds
in `preflight.yaml`, and returns a `DiffReport`. `preflight diff --baseline
<sha> --candidate <sha>` renders it through M4's `report.render_markdown`,
supports `--format markdown|json` and `--output <path>`, and exits 1 on breach.
Thresholds for all six metrics are in `preflight.yaml` with their reasoning.
`scripts/m3_check.py` is the acceptance check and calls no model.

**Measure the variance, don't guess it.** The plan says to set thresholds "well
above observed run-to-run variance", so the first move was to run the real suite
twice under two SHAs and diff it: 0.0% on cost, tokens, tool calls and hops
(cassette replay makes those exact), 0.2% on latency. Thresholds sit at 25–75%,
which is 100×+ the measured noise — and they are sized for re-recorded
cassettes, not for this 0%.

**Gotcha, and it is a nasty one: an unqualified attribute name in a SigNoz
filter expression resolves to the _resource_ attribute, not the span
attribute.** `vcs.commit_sha = 'abc'` matched 0 spans while
`attribute.vcs.commit_sha = 'abc'` matched 42, because `otel.py` stamps the
resource once per process. In ordinary CI — one process, one commit — the two
values agree and the bug is invisible. `groupBy` is immune because it takes an
explicit `fieldContext`, which is exactly why the M1 probe looked fine. Qualify
every filter.

**Gotcha: percentages are meaningless near zero, and a gate that cries wolf gets
switched off.** The first end-to-end run reported a +205% p95 latency regression
between two *identical* runs, because the synthetic cases took 0.12ms. Fixed
with an absolute noise floor (`p95_latency_ms_abs_floor_ms: 25`): a rise must
clear 25ms as well as the percentage. Latency is the only metric with a
near-zero regime, so it is the only one with a floor.

**Gotcha: `max(timestamp)` works and returns epoch seconds**, so SHA → newest
run is one scalar query rather than a bisection over time windows. But string
intrinsics can't be aggregated at all — `any()` is not a recognised function and
`max(trace_id)` makes ClickHouse try to cast hex to Float64. Trace ids need a
`fieldContext: "span"` group-by instead.

**Gotcha: a reader's default lookback silently truncates a valid diff.**
`query.run_summary_typed` defaults to 60 minutes; `resolve_run` will happily
find a day-old baseline. Not threading the window through turns "your baseline
is from yesterday" into "baseline run has no cases".

**Takeaway.** Every real bug in this milestone was a *silent* one — a filter that
matched the wrong field, a percentage of a near-zero number, a window that
quietly excluded the data. None of them threw. The offline fixture tests caught
none of them and the single end-to-end run against real SigNoz caught all three,
which is the argument for running the acceptance check the moment the milestone
ends rather than batching it to the morning.

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
