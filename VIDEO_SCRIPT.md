# Video script — Preflight (target 2:45, hard cap 3:00)

Everything below is verified live as of recording prep. **Narration is written to
be read aloud** — the word counts are timed for a normal speaking pace, so if you
finish a shot early, hold the frame rather than filling.

---

## Before you hit record

```bash
make up                                   # stack healthy
make signoz-apply                         # dashboards present
make ci-local BRANCH=seeded-regression    # warms traces so links resolve; exits 1
make m6-check                             # note the trace URL it prints
```

**Open these tabs, in this order** (so you can move left-to-right, no fumbling):

| Tab | URL |
|---|---|
| 1 | https://github.com/ishanavasthi/preflight/pull/1/files |
| 2 | https://github.com/ishanavasthi/preflight/pull/1 |
| 3 | Terminal, in the repo, cleared, large font |
| 4 | http://localhost:8080/dashboard/019fa222-031a-7831-8118-4f74ac580124 (Cost per Task by Commit) |

> ⚠️ **Never click a trace link inside the GitHub comment on camera.** CI stands up
> its own SigNoz inside the runner and tears it down when the job finishes — those
> trace IDs no longer exist. Every clickable link in this script comes from
> `make ci-local` or `make m6-check`, against your local stack. Verified.

---

## Shot 1 — the innocent PR · 0:00–0:22 · Tab 1

**On screen:** the Files-changed view. Nine lines of a system prompt. Scroll
slowly so the diff is legible, then scroll to the green checks.

> "This pull request makes a customer-support agent a bit more thorough. It's a
> nine-line prompt change. The tests pass, lint is clean — I'd approve this."

*(beat)*

> "It also makes the agent three times more expensive per task. That's not in the
> diff, and nothing in normal CI has an opinion about it."

---

## Shot 2 — the gate catches it · 0:22–0:52 · Tab 2

**On screen:** scroll to the red **Preflight** check, then to the bot comment.
Let the table hold still for a good three seconds — this is the money shot. Don't
narrate over the pause.

> "Preflight is a CI gate for agent behaviour. Here's what it posted."

*(hold — let them read)*

> "Cost per task up a hundred and fifty percent. Twice the retrieval hops. And
> it's calling a tool it never touched before. Five metrics breached, the check
> fails, and the PR is blocked."

---

## Shot 3 — the design bet · 0:52–1:18 · Tab 3

**On screen:** run this, and let the JSON sit there while you talk.

```bash
cat preflight/query.py | sed -n '/def scalar/,/^        }/p' | head -30
```

*(or just scroll `preflight/query.py` — the point is the query shape)*

> "Every number in that comment came out of SigNoz's query API. There's no
> results file, no JSON artifact — SigNoz **is** the datastore for the gate."

> "The whole design rested on one question: can the query API group by a custom
> span attribute? Not a resource attribute — an arbitrary `eval.case_id` I made
> up. It can. That's one row per case, per commit, in a single round trip, and
> everything else follows from it."

---

## Shot 4 — the same gate, locally, with working links · 1:18–1:55 · Tab 3 → browser

**On screen:** run it. Let it scroll. When the comment renders, **click a per-case
trace link** — pick `damaged-item`, the biggest mover.

```bash
make ci-local BRANCH=seeded-regression
```

> "Same gate, run locally. It checks out the merge base and the branch head, runs
> the suite against each, and diffs them."

*(when the table appears)*

> "Identical comment. And every row links to that case's trace…"

*(click the `damaged-item` trace link → SigNoz waterfall opens)*

> "…straight to the waterfall. There's the agent's run — the model calls, the
> tool calls, the retrieval hop. The span that explains the regression is one
> click from the pull request."

**If the link 404s:** you skipped `make ci-local` in prep, or the stack restarted.
Re-run it and use a trace ID from the fresh output.

---

## Shot 5 — the full circle · 1:55–2:28 · Tab 3 → browser

**On screen:** run it, let the diagnosis text render, then open the trace URL it
prints at the end.

```bash
make m6-check
```

> "When the gate fails, a diagnosis agent investigates — over the SigNoz MCP
> server."

*(when the diagnosis text appears, pause and let it read)*

> "It names the worst case, quotes the deltas, and cites a tool and a retrieval
> source that appear nowhere in its prompt. Those exist only as span attributes —
> so it could only have got them by querying SigNoz."

*(open the printed trace URL → SigNoz waterfall of `preflight-diagnose`)*

> "And its own investigation is a trace in SigNoz. Twenty-two spans — its
> reasoning turns, and every MCP call it made. An agent debugging an agent, both
> observable in the same place."

---

## Shot 6 — dashboards and alerts as code · 2:28–2:42 · Tab 4

**On screen:** the Cost per Task by Commit dashboard. Scroll once.

> "Dashboards and alert rules are committed JSON, applied idempotently through
> the MCP server. Delete one from the UI, run `make signoz-apply`, and it comes
> back identical."

---

## Shot 7 — close · 2:42–2:55 · back to Tab 2

**On screen:** the red check at the top of the PR.

> "Nine lines of prompt. Five breached metrics. Caught on the pull request that
> caused it, with the span that explains it one click away."

*(beat)*

> "That's Preflight."

---

## Notes for the edit

- **Shot 2 is the one that sells it.** If you're over three minutes, cut narration
  from shots 3 and 6, never from shot 2.
- If shots 4 and 5 run slow live, **pre-run them and cut to the finished output** —
  nobody needs to watch `uv sync`.
- If you need 20 seconds back: shot 6 can drop to a 4-second silent pan with no
  narration, and shot 3 can lose its second paragraph.
- Say **"SigNoz"** clearly in shots 3, 5 and 6 — "Best Use of SigNoz" is a judged
  criterion and the audio is evidence.
- Don't say "we" — you're solo, and the AI-assistance disclosure is in the README
  and the form where it belongs.

## Word count

~330 words of narration ≈ 2:15 spoken, leaving ~40s of held frames and pauses.
That is deliberate: the tables need silence to be read.
