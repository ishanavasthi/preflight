# Video script — Preflight (target 2:50, hard cap 3:00)

**Read the narration out loud before you record.** It's written the way you'd
actually talk — short sentences, contractions, no jargon that isn't earned. If a
line feels stiff in your mouth, say it your own way. The *ideas* in bold below
are what has to land; the exact words don't.

**The three ideas, in priority order.** If everything else falls apart, land these:

1. A nine-line prompt change made this agent 3× more expensive — and every test passed.
2. Preflight catches it on the PR, and every number came out of SigNoz. There's no results file — **SigNoz is the datastore**.
3. Click any row, land on the trace that explains it.

---

## Before you hit record

Your stack is up and the dashboards are applied, so `make up` and
`make signoz-apply` are already satisfied. The other two aren't prerequisites —
they're shots. But **pre-run them anyway**:

```bash
make ci-local BRANCH=seeded-regression    # ~2-3 min. Warms the worktrees.
make m6-check                             # 30s. Note the trace URL it prints.
```

Two reasons. It's your rehearsal, and it makes the on-camera runs fast — the
worktrees and the `uv` cache are warm the second time.

⚠️ **Timing reality:** `make ci-local` takes 2–3 minutes and shot 4 is 36
seconds. `make m6-check` takes 30 seconds against shot 5's 34. **Start the
command on camera, then cut to the finished output.** Standard demo edit, nobody
blinks. Don't try to run either one live in full.

**Open these tabs, in this order** (so you move left-to-right, no fumbling):

| Tab | URL |
|---|---|
| 1 | https://github.com/ishanavasthi/preflight/pull/1/files |
| 2 | https://github.com/ishanavasthi/preflight/pull/1 |
| 3 | Terminal, in the repo, cleared, large font |
| 4 | http://localhost:8080/dashboard/019fa222-031a-7831-8118-4f74ac580124 (Cost per Task by Commit) |

> ⚠️ **Never click a trace link inside the GitHub comment on camera.** CI stands
> up its own SigNoz inside the runner and tears it down when the job finishes —
> those trace IDs no longer exist. Every clickable link in this script comes from
> `make ci-local` or `make m6-check`, against your local stack. Verified.

---

## Shot 1 — the problem, then the PR · 0:00–0:30 · Tab 1

**On screen:** start on the Files-changed view. Scroll the nine-line prompt diff
slowly enough to read, then scroll down to the green checks and let them sit.

> "This is a customer-support agent. Someone's opened a pull request that changes
> nine lines of its prompt — basically, *be more thorough before you answer.*"

*(scroll to the green checks)*

> "Tests pass. Lint's clean. I'd approve this."

*(beat — this is the turn)*

> "But it now costs three times more per question, and it's calling a tool it's
> never touched. None of that is in the diff, and nothing in normal CI has an
> opinion about it. You'd find out in next month's bill."

---

## Shot 2 — the gate catches it · 0:30–1:00 · Tab 2

**On screen:** scroll to the red **Preflight** check, then to the bot comment.
Let the table hold still for a good three seconds. **This is the money shot —
don't narrate over the pause.**

> "Preflight is a CI check for agent behaviour. It ran the same six tasks against
> both sides of this pull request. Here's what it posted."

*(hold — let them read the table)*

> "Cost per task, up a hundred and fifty percent. Twice the retrieval hops. And a
> tool call that wasn't there before. Five metrics breached, so the check fails
> and the PR is blocked."

---

## Shot 3 — why SigNoz · 1:00–1:22 · Tab 3

**On screen:** scroll `preflight/query.py` slowly. The point is the *shape* of
the query, not any specific line — nobody's reading it, they're seeing that a
query exists where a JSON file would be.

> "Here's the design bet. There's no results file. Every number in that comment
> came back out of SigNoz's query API."

> "The agent already emits traces, so the trace store already has everything the
> gate needs. Writing a second copy to a JSON file would just drift. SigNoz **is**
> the datastore for the gate, not a dashboard bolted on the side."

*(If you're running long, cut the second paragraph down to the last sentence.)*

---

## Shot 4 — the same gate, locally, with links that work · 1:22–1:58 · Tab 3 → browser

**On screen:** start the command, **cut to the rendered comment**. Then click a
per-case trace link — pick `damaged-item`, the biggest mover.

```bash
make ci-local BRANCH=seeded-regression
```

> "Same gate, running locally. It checks out the merge base and my branch, runs
> the suite against each, and diffs them."

*(cut to the table)*

> "Same comment. And every row links to that case's trace…"

*(click the `damaged-item` trace link → SigNoz waterfall opens)*

> "…and there's the actual run. The model calls, the tool calls, the extra
> lookup. The span that explains the regression is one click from the pull
> request."

**If the link 404s:** the stack restarted since your prep run. Re-run
`make ci-local` and use a trace ID from the fresh output.

---

## Shot 5 — an agent debugging an agent · 1:58–2:32 · Tab 3 → browser

**On screen:** start the command, cut to the rendered diagnosis, then open the
trace URL it prints at the end.

```bash
make m6-check
```

> "When the gate fails, a second agent goes and investigates — over the SigNoz
> MCP server."

*(cut to the diagnosis text, pause and let it read)*

> "It names the worst case and quotes the deltas. But look — it mentions a tool
> and a knowledge source that appear nowhere in its prompt. Those only exist as
> span attributes. It could only have got them by querying SigNoz."

*(open the printed trace URL → SigNoz waterfall of `preflight-diagnose`)*

> "And its own investigation is a trace too. Twenty-two spans. An agent debugging
> an agent, both observable in the same place."

⚠️ **The trace ID changes on every run.** Read it off the terminal during the
take — don't pre-write it.

---

## Shot 6 — dashboards and alerts as code · 2:32–2:44 · Tab 4

**On screen:** the Cost per Task by Commit dashboard. One slow scroll.

> "Dashboards and alert rules are committed JSON, applied through the MCP server.
> Delete one from the UI, run `make signoz-apply`, and it comes back identical."

---

## Shot 7 — close · 2:44–2:55 · back to Tab 2

**On screen:** the red check at the top of the PR.

> "Nine lines of prompt. Five breached metrics. Caught on the pull request that
> caused it, with the trace that explains it one click away."

*(beat)*

> "That's Preflight."

---

## Notes for the edit

- **Shot 1's turn and shot 2's table are the video.** Everything else is
  supporting evidence. If you're over three minutes, cut from 3 and 6 — never
  from 1 or 2.
- Shot 6 can drop to a 4-second silent pan with no narration. That's 10 seconds
  back if you need it.
- Say **"SigNoz"** clearly in shots 3, 5 and 6. "Best Use of SigNoz" is a judged
  criterion and your audio is the evidence.
- Don't say "we" — you're solo. The AI-assistance disclosure lives in the README
  and the form, which is where it belongs.
- Don't apologise for cuts or say "I pre-ran this." Just show the output.

## Word count

~360 words of narration ≈ 2:20 spoken, leaving ~35s of held frames and pauses.
That's deliberate — the tables need silence to be read.
