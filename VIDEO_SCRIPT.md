# Video script — Preflight (~2:45, hard cap 3:00)

**No source code on screen anywhere.** The only diff you show is the agent's
*instructions* — plain English inside a string. That's the point: a viewer can
read the change, then watch its effect in a trace, and connect the two without
knowing any Python.

## What the video has to land

1. Someone rewrote an agent's instructions. Every test passed.
2. Preflight ran the agent's real tasks on both sides of the PR and blocked it — **cost per task up 150%**.
3. The trace shows *exactly* the steps the new instructions asked for. Cause in the diff, effect in the trace.

---

## Before you record

**Run these in order.** The first one matters most — SigNoz's trace view defaults
to a recent time window, so traces from two hours ago may not show up.

```bash
make ci-local BRANCH=seeded-regression   # ~2-3 min. Makes fresh traces + the report.
make m6-check-live                       # ~30s, costs ~$0.05. See the warning below.
```

⚠️ **Use `m6-check-live`, not `m6-check`.** The replay cassette for the diagnosis
agent only hits when SigNoz holds the exact data it was recorded against, and it
does not right now — `make m6-check` currently fails with a cassette miss. The
live version always works and is more impressive anyway (a real investigation,
not a replay). At ~$0.05 a run and ~$0.72 left in the budget, you have room for
about a dozen takes.

Then get the two trace URLs for scene 3 — **copy this whole block**:

```bash
docker exec signoz-telemetrystore-clickhouse-0-0 clickhouse-client -q "
SELECT substring(resources_string['vcs.commit_sha'],1,8) AS commit,
       count() AS spans,
       concat('http://localhost:8080/trace/', trace_id) AS url
FROM signoz_traces.distributed_signoz_index_v3
WHERE attributes_string['eval.case_id']='damaged-item'
GROUP BY trace_id, commit ORDER BY max(timestamp) DESC LIMIT 2 FORMAT TSV"
```

It prints two lines. The one with **~5 spans is the baseline**, the one with
**~13 spans is the candidate**. You'll open both.

### Windows to have open, in the order you'll use them

| # | Screen | Where |
|---|---|---|
| 1 | The instructions diff | `https://github.com/ishanavasthi/preflight/pull/1/files#diff-159d14566d294f190bf4c63eb7d45e2e269867afc880d98bc0ba1288fb04606e` |
| 2 | The PR comment | `https://github.com/ishanavasthi/preflight/pull/1` |
| 3 | Trace — **before** | the ~5-span URL from the command above |
| 4 | Trace — **after** | the ~13-span URL from the command above |
| 5 | Terminal | `make m6-check-live` output already printed, scrolled to the diagnosis |
| 6 | Trace — the diagnosis agent | the URL `m6-check-live` prints at the very end |
| 7 | Dashboard | `http://localhost:8080/dashboard/019fa222-031a-7831-8118-4f74ac580124` |

The window-1 link jumps straight to `agent/reference.py`. **You need it** — the
PR also contains 21 recorded-response JSON files, and without the anchor you land
in a wall of JSON.

> ⚠️ **Never click a trace link inside the GitHub comment.** CI built its own
> SigNoz inside the runner and threw it away. I checked: those trace IDs have
> **zero rows** in your local database. They will 404 on camera. Windows 3, 4 and
> 6 are your local traces and they work.

---

## Scene 1 — the change · 0:00–0:32 · window 1

**On screen:** the red/green diff of the agent's instructions. Scroll slowly
enough that the green block is readable. Then scroll down to the green check marks.

> "This is a customer-support agent, and this pull request makes it more careful.
> The change is just the agent's instructions — plain English, not code."

*(let the diff sit — the green block is the whole setup)*

> "Before, it said: check the facts, answer in two sentences. Now it says: work
> one step at a time. Search the policy. Look up the order. Check inventory.
> Search the policy again. Then list every option."

*(scroll to the green checks)*

> "Tests pass. I'd approve this."

---

## Scene 2 — the check that failed · 0:32–1:05 · window 2

**On screen:** scroll to the red **Preflight** check, then to the comment. Let
the table hold still. **Don't talk over the pause** — this is the shot that sells it.

> "But Preflight ran the agent's real tasks against both sides of this pull
> request. Here's what it posted."

*(hold, 4 seconds, silent)*

> "Cost per task, up a hundred and fifty percent. Tokens, up a hundred and thirty.
> Latency, up a hundred and sixty-seven. Five metrics breached — so the check
> fails and the pull request is blocked."

---

## Scene 3 — cause and effect · 1:05–1:50 · windows 3 → 4

**This is the best 45 seconds in the video.** Same test case, before and after,
side by side. Switch between the two windows so the waterfalls visibly differ in
length.

**On screen:** window 3 first — a short waterfall. Then window 4 — a long one.
Scroll down window 4 so the `execute_tool` rows are visible.

> "Every number in that comment came out of SigNoz. Here's one test case before
> the change…"

*(window 3)*

> "…five spans. Two seconds. One tool call."

*(switch to window 4 — let the difference land)*

> "…and after. Thirteen spans. Eight and a half seconds."

*(scroll so the tool rows show; trace them with the cursor as you say them)*

> "And look at what it's doing. `policy_search`. `lookup_order`.
> `check_inventory`. `policy_search` again. That's exactly the four steps the new
> instructions asked for — you can read the cause in the diff and watch the effect
> in the trace."

---

## Scene 4 — an agent debugging an agent · 1:50–2:22 · window 5 → 6

**On screen:** the terminal, already showing the diagnosis paragraph. Don't run
anything live. Then open window 6.

> "When the gate fails, a second agent investigates — over the SigNoz MCP server."

*(let the diagnosis text sit for a beat)*

> "It finds the worst case and explains it in English. And it names
> `policy_search` and `policy-kb` — which appear nowhere in its prompt. Those
> exist only inside the traces. It could only have got them from SigNoz."

*(window 6 — the diagnosis agent's own trace)*

> "And its own investigation is a trace too. Every reasoning turn, every query it
> made. An agent debugging an agent, both observable in the same place."

---

## Scene 5 — dashboards, and close · 2:22–2:45 · window 7 → window 2

**On screen:** the cost-per-task dashboard, one slow scroll. Then cut back to the
red check at the top of the PR.

> "Dashboards and alert rules are committed JSON, applied through the MCP server."

*(cut back to the red X)*

> "Nine lines of instructions. Five breached metrics. Caught on the pull request
> that caused it, with the trace that explains it one click away."

*(beat)*

> "That's Preflight."

---

## Notes for the edit

- **Scene 3 is the one to protect.** If you're over time, cut narration from
  scene 5, then scene 4 — never from 1, 2 or 3.
- Don't read span counts off my script — read them off *your* screen. They shift
  slightly between runs.
- Say **"SigNoz"** clearly in scenes 3, 4 and 5. "Best Use of SigNoz" is judged
  and your audio is the evidence.
- Don't say "we" — you're solo. The AI-assistance disclosure belongs in the
  README and the form, and it's already in both.
- Nothing is run live on camera. Don't apologise for that or mention it — just
  show the output.

## Word count

~355 words ≈ 2:20 spoken, leaving ~25s of held frames. The pauses in scenes 2 and
3 are load-bearing: the table and the two waterfalls need silence to be read.
