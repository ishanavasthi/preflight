# Your agent got 3× more expensive and your tests are still green

*Building a CI gate for AI agents, with SigNoz as the source of truth.*

---

Here is a pull request. It changes nine lines of a system prompt — something
about being more thorough before answering. The unit tests pass. Coverage is
unchanged. Lint is clean. You'd approve it.

It also makes the agent cost **150% more per task**, take **twice as many
retrieval hops**, and start calling a tool it never touched before.

None of that is in the diff, and none of it is in your CI. The code didn't change
shape — same functions, same signatures, same assertions. What changed is
behaviour, and behaviour isn't something a test suite has an opinion about unless
you build one.

You find out in next month's invoice. Or in a latency alert at 2am.

I spent a night building the thing that catches it.

---

## The bet

The obvious design is: run the agent, write results to a JSON file, compare
against a stored baseline, fail the build. That works, and it's boring, and it
throws away the most interesting property of the problem.

Agent runs are already *traces*. A task is a root span, each model call is a
child, each tool call is a child of that. If you're going to instrument the agent
anyway — and you are, because you'll want to debug it — then the trace store
already contains everything the gate needs. Writing a second, parallel record in
a JSON file is duplication that will drift.

So: **SigNoz is the datastore for the gate, not a dashboard bolted on the side.**
No local results file. Every number in the PR comment comes back out of
`POST /api/v5/query_range`. If SigNoz can't answer, the gate fails loudly rather
than guessing.

That rested on one question I couldn't answer from the docs: *can the query API
aggregate and group by a custom span attribute?* Not a resource attribute, not a
well-known field — an arbitrary `eval.case_id` I made up. If the answer was no,
the design was dead and I needed to know in the first hour, not the fifth.

It's yes:

```jsonc
"aggregations": [{"expression": "sum(preflight.cost_usd)"}],
"filter":       {"expression": "attribute.vcs.commit_sha = '59607e52…'"},
"groupBy":      [{"name": "eval.case_id", "fieldContext": "attribute"}]
```

One row per case, per commit, in a single round trip. Everything else in the
project follows from that being true.

---

## Every bug that mattered was silent

I want to dwell on this, because it turned out to be the shape of the whole
night. Not one of the real bugs threw an exception. Every one of them returned a
plausible number.

**An unqualified attribute name resolves to the wrong field.** `vcs.commit_sha =
'abc'` matched zero spans. `attribute.vcs.commit_sha = 'abc'` matched forty-two.
The unqualified form silently resolves to the *resource* attribute, which my
setup also happens to stamp — so in ordinary CI, one process and one commit, the
two values agree and the bug is invisible. It only surfaces when they diverge.
`groupBy` is immune because it demands an explicit `fieldContext`, which is
exactly why my first probe looked fine and gave me false confidence.

**A percentage of a near-zero number is not information.** The first end-to-end
run reported a +205% latency regression between two *identical* runs, because the
cases took 0.12ms. A gate that cries wolf gets switched off by the third
engineer who hits it. Latency now needs to clear an absolute floor as well as a
percentage.

**A reader's default lookback window silently truncated valid diffs.** The
summary reader defaulted to sixty minutes; the SHA resolver would happily find a
baseline run from yesterday. The result wasn't an error, it was "baseline run has
no cases" — which reads like a different problem entirely.

The offline fixture tests caught none of these. A single run against real SigNoz
caught all three. That's the argument for running each milestone's acceptance
check the moment the milestone ends instead of batching verification to the
morning: fixtures test the code you wrote, and these were all bugs in the code I
*thought* I'd written.

---

## The one that only appeared in CI

The gate worked locally, first try, every time. Then I opened a real pull
request and it failed with `0/32 spans`.

Not a partial count. Zero. Meanwhile every health check was green — SigNoz's
`/api/v1/health` returning 200 the whole way through.

The collector logs had it. About thirty-five seconds into the run:

```
"msg":"Shutdown complete."
"msg":"Starting collector service"
```

SigNoz's collector fetches its configuration over opamp shortly after boot and
**restarts**. Anything exported during that window is accepted over OTLP —
returns 200, no error, no warning — and then dropped. Locally this never bit me
because SigNoz had been running for an hour before I ever ran the suite. It only
happens on a cold deployment, which is precisely what CI is.

The fix isn't a longer timeout. It's gating on the property you actually depend
on: write a probe span, then poll the query API until it reads back. Health means
"the API is up." It does not mean "the pipeline persists data," and those are
different claims.

There's a general version of this. My runner already had an ingest poller —
after flushing, it waits until the run's spans are queryable and **fails loudly**
rather than diffing a partial run. I built it because reading too early would
produce a silently wrong verdict, which is the worst failure mode available when
nothing looks broken. It earned its keep before it ever reached CI: its first act
was to report `0/6` and catch a bug in my own response parser, which would
otherwise have returned an empty list that downstream code would have read as
*no regression*.

A component that fails loudly on the expected failure mode also surfaces the
unexpected ones.

---

## Making an agent explain itself

The last piece is a diagnosis agent. When the gate fails, it investigates the
traces over the **SigNoz MCP server** and explains what happened in English:

> The **damaged-item case** is the worst performer, with cost increasing from
> $0.00246 to $0.00946 (+284%), tokens from 2,016 to 7,116 (+253%)… the candidate
> now calls `policy_search` … Retrieval hops to policy-kb doubled from 6 to 14.

And it is itself instrumented, so its investigation lands in SigNoz as a 22-span
trace. One failed gate produces the agent's traces, the gate's queries against
them, and the diagnosis agent's own reasoning — all in one place. An agent
debugging an agent, both observable.

Writing the acceptance check for that taught me the most useful thing of the
night. My first instinct was "assert the explanation names the worst case." That
check is nearly worthless: the gate's per-case table is *in the agent's prompt*.
A model that called nothing and read nothing could pass it by paraphrasing its
own input.

What makes the check real is that `policy_search` and `policy-kb` appear nowhere
in the prompt. They exist only as span attributes. Quoting them is proof the
agent went to SigNoz and came back.

> **When you test a generated explanation, find a fact that lives only on the far
> side of the tool call, and assert on that. Everything else measures fluency.**

That check immediately earned its keep. I A/B'd a tightened prompt that demanded
the model name a concrete root cause and rule out alternatives. It reasoned
*visibly better* — correctly identified a prompt edit, explicitly excluded a
model swap because every span still showed the same model. It also stopped
grouping by tool name, and asserted "the same tools appear in both runs," which
is flatly false.

The check failed it. Same model, same data, same tools: instructing it harder
about *what to conclude* pulled effort away from *gathering what the conclusion
rests on*. I kept the looser prompt and recorded the A/B next to it so nobody
"improves" it back.

---

## What it actually does now

Open a PR. CI stands up SigNoz inside the job, runs a six-case golden suite
against both the merge base and your branch, queries per-commit aggregates, and
posts a comment:

| | Metric | Baseline | Candidate | Δ | Threshold |
|:--:|---|---:|---:|---:|---:|
| ❌ | Cost / task | $0.0026 | $0.0064 | ▲ +150.5% | +25% |
| ❌ | Tokens / task | 2,034 | 4,670 | ▲ +129.7% | +25% |
| ❌ | p95 latency | 3.18s | 8.51s | ▲ +167.4% | +75% |
| ❌ | Tool calls / task | 1.33 | 3 | ▲ +125.0% | +40% |
| ❌ | Retrieval hops / task | 1 | 2.33 | ▲ +133.3% | +50% |
| ✅ | Success rate | 100% | 100% | — | drop > 1 pts |

The check fails. Each row links to that case's trace waterfall. Dashboards and
alert rules are committed JSON, applied idempotently through MCP.

Nine lines of prompt. Five breached metrics. Caught on the pull request that
caused it.

---

## What I'd do differently

**I'd spend the first hour on the interfaces, not the first feature.** Three
milestones got built in parallel by separate agents only because I stopped and
wrote down the seams first — one dataclass for what the query layer returns, one
for what the gate produces. The metric arithmetic lives in exactly one place, so
the gate and the renderer cannot disagree about what "cost per task" means. That
half hour is why the rest was concurrent instead of serial.

**I'd trust confident error messages less.** Late on, checking a claim before
publishing it, SigNoz told me `preflight.case.cost_usd` "has never been received
— check the metric name and instrumentation." The instrumentation was correct. An
OpenTelemetry histogram is decomposed on ingest into `.sum`, `.count`, `.bucket`
— and the base name exists as nothing at all. I was one step from deleting a true
claim about working code on the strength of a warning that was, in fact, a
statement about a *name*.

Verification has to run in both directions. Not only "is this claim too strong?"
but "is this failure real?"

---

*Built with Claude Code (Claude Opus 5), disclosed per the hackathon's AI-use
protocol. Source: [github.com/ishanavasthi/preflight](https://github.com/ishanavasthi/preflight)*
