# Problem

## The problem

A team ships an AI agent behind a PR workflow. Someone edits one line of a
prompt, swaps a tool description, or bumps a model. The unit tests still pass —
the code didn't change shape. What changed is invisible: the agent now makes two
extra retrieval hops, reaches for a different tool, and costs 3× per task.

Nobody sees it at review time. They see it in next month's invoice, in a latency
alert at 2am, or in a user complaint.

**Solved means:** the regression is caught in CI, on the PR that caused it, with
the specific span that explains it one click away.

## Goals

- A PR that degrades the agent fails CI, and the PR comment names the metric,
  the delta, and links to the exact LLM span in SigNoz.
- A PR that doesn't degrade it passes, with the same numbers shown as a green diff.
- A judge clones the repo, runs `foundryctl cast -f casting.yaml`, and reproduces
  the whole stack — SigNoz, MCP server, dashboards, alerts — without asking a
  question.
- Every number in the gate came out of SigNoz's query API, not a local JSON file.

## Scope

In: the reference agent under test, OTel instrumentation to GenAI semconv, the
golden suite, the differ and its thresholds, the GitHub Action and PR comment,
dashboards and alerts as code, and a diagnosis agent over the SigNoz MCP server.

Explicitly out (v1): chaos/fault injection, multi-language agent support, a web
UI, auth/multi-tenancy, historical trend backfill or statistical significance
testing, and hosted deployment. Changing this list is a logged decision, not
silent drift.

## Constraints

- **Hackathon**, solo, one night. Track 1 — AI & Agent Observability.
- Must use or integrate with SigNoz; depth of use is a judged criterion.
- **Field Requirement 3:** the repo must include `casting.yaml` and
  `casting.yaml.lock`; judges may re-run Foundry against them.
- **Agency Protocol 7:** AI assistance must be declared. Built with Claude Code;
  disclosed in the README and on the submission form.
- **Agency Protocol 8:** planning was written pre-kickoff; code started at kickoff.
- No Anthropic API key available on the build machine — model access is via
  OpenRouter (see `approach.md`).
