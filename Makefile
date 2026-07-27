SHELL := /bin/bash
.DEFAULT_GOAL := help

# OrbStack ships the docker CLI inside its app bundle and only symlinks it into
# PATH after the GUI first-run completes. Add it unconditionally -- harmless
# when docker is already on PATH.
export PATH := $(HOME)/.local/bin:/Applications/OrbStack.app/Contents/MacOS/xbin:$(PATH)

.PHONY: help up down logs bootstrap install run query check clean \
        report-sample verify-links lint-ci ci-local \
        signoz-apply signoz-apply-check signoz-verify-panels signoz-diff \
        explain explain-dry m6-check m6-check-live mcp-tools

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up: ## Bring the SigNoz stack up via Foundry (writes casting.yaml.lock)
	foundryctl cast -f casting.yaml

down: ## Tear the stack down
	docker compose -p signoz -f pours/deployment/compose.yaml down

logs: ## Tail the collector logs
	docker logs -f signoz-ingester-1

bootstrap: ## Create the admin user + API key, write .env
	./scripts/bootstrap_signoz.sh

install: ## Sync the Python environment
	uv sync

run: ## Run the golden suite and wait for SigNoz ingest
	set -a && . ./.env && set +a && uv run preflight run

query: ## Summarise a run: make query RUN_ID=run-xxxx
	@test -n "$(RUN_ID)" || { echo "usage: make query RUN_ID=run-xxxx"; exit 1; }
	set -a && . ./.env && set +a && uv run preflight query --run-id $(RUN_ID)

check: ## M1 acceptance check: run one case, then read it back from SigNoz
	@set -a && . ./.env && set +a && \
	out=$$(uv run preflight run --cases 1 2>&1) && \
	echo "$$out" | grep -vE '^  [0-9]+/[0-9]+ spans visible$$' && \
	rid=$$(echo "$$out" | awk '/^run_id/{print $$2}') && \
	echo "" && uv run preflight query --run-id $$rid

clean: ## Remove the stack and all telemetry volumes
	docker compose -p signoz -f pours/deployment/compose.yaml down -v

# --- M4: the PR comment and the GitHub Action -------------------------------

report-sample: ## Render a sample PR comment from a synthetic DiffReport
	@set -a && . ./.env && set +a && uv run python scripts/report_sample.py

verify-links: ## Prove every SigNoz deep link in the comment actually resolves
	@set -a && . ./.env && set +a && uv run python scripts/report_sample.py --verify

lint-ci: ## Lint the GitHub workflow (actionlint + shellcheck)
	@command -v actionlint >/dev/null || { echo "actionlint not installed: brew install actionlint"; exit 1; }
	actionlint .github/workflows/preflight.yml && echo "workflow OK"

# Rehearses the whole Action on this machine: two detached worktrees at the
# merge base and the branch head, the suite run against each in replay mode,
# then the gate. `make ci-local BRANCH=seeded-regression` should exit 1 with a
# report; against a branch that changes nothing it should exit 0.
# --- M5: dashboards and alerts as code --------------------------------------
# Applied through the SigNoz MCP server, not the REST API. dashboards/*.json and
# alerts/*.json are the literal MCP tool arguments, so the committed file is the
# payload -- there is no translation layer to drift.

signoz-apply: ## Apply dashboards/ + alerts/ to SigNoz through MCP (idempotent)
	@set -a && . ./.env && set +a && uv run python scripts/signoz_apply.py

signoz-diff: ## Show what signoz-apply would change, without changing it
	@set -a && . ./.env && set +a && uv run python scripts/signoz_apply.py --dry-run

signoz-verify-panels: ## Execute every committed panel query; fail if one returns no data
	@set -a && . ./.env && set +a && uv run python scripts/signoz_apply.py --verify

# BUILD_PLAN's M5 check, automated: delete a dashboard out from under SigNoz,
# re-apply, and assert the definition that comes back is byte-identical to the
# one that was there before. Proves the committed JSON -- not the live
# deployment -- is the source of truth.
signoz-apply-check: ## M5 acceptance check: delete a dashboard, re-apply, prove it returns identical
	@set -a && . ./.env && set +a && uv run python scripts/signoz_apply_check.py

ci-local: ## Dry-run the CI gate locally: make ci-local BRANCH=seeded-regression
	@test -n "$(BRANCH)" || { echo "usage: make ci-local BRANCH=<branch>"; exit 1; }
	@set -euo pipefail; \
	base=$$(git merge-base main $(BRANCH)); \
	cand=$$(git rev-parse $(BRANCH)); \
	echo "baseline  $$base"; echo "candidate $$cand"; \
	rm -rf /tmp/pf-ci-base /tmp/pf-ci-cand; \
	git worktree add --detach /tmp/pf-ci-base $$base >/dev/null; \
	git worktree add --detach /tmp/pf-ci-cand $$cand >/dev/null; \
	set -a; . ./.env; set +a; unset ANTHROPIC_API_KEY; \
	export PREFLIGHT_REPLAY=1 PREFLIGHT_CASSETTES=/tmp/pf-ci-cand/.cassettes; \
	for pair in "base:$$base" "cand:$$cand"; do \
	  d=$${pair%%:*}; sha=$${pair#*:}; \
	  echo "--- suite @ $$sha ---"; \
	  ( cd /tmp/pf-ci-$$d && uv sync -q && PREFLIGHT_COMMIT_SHA=$$sha uv run preflight run ); \
	done; \
	echo "--- gate ---"; \
	set +e; \
	uv run preflight diff --baseline $$base --candidate $$cand \
	  --format markdown --output /tmp/preflight-report.md; \
	code=$$?; \
	echo "preflight diff exited $$code"; \
	cat /tmp/preflight-report.md; \
	uv run python scripts/report_sample.py --check /tmp/preflight-report.md; \
	git worktree remove --force /tmp/pf-ci-base; \
	git worktree remove --force /tmp/pf-ci-cand; \
	exit $$code

# --- M6: the diagnosis agent over MCP ---------------------------------------
# The gate says a PR made the agent worse. This says *why*, in English, using
# the SigNoz MCP server as its only source of facts -- and its own investigation
# lands in SigNoz as a trace, which is the milestone's whole point.
#
# BUILD_PLAN calls this `preflight explain`; it ships as `python -m
# preflight.diagnose` because preflight/cli.py was frozen while M6 was built.
# Wiring it into the CLI is a one-line `cli.add_command` follow-up.

BASELINE ?= e0592cf84cc63ee4f3a6c1d0435b42d48df52728
CANDIDATE ?= 59607e52008b29a41f9722671f3e7a4f61914b61

mcp-tools: ## List every tool the SigNoz MCP server advertises
	@set -a && . ./.env && set +a && uv run python -m preflight.mcp list

explain: ## Diagnose a failed gate: make explain BASELINE=<sha> CANDIDATE=<sha>
	@set -a && . ./.env && set +a && \
	uv run python -m preflight.diagnose --baseline $(BASELINE) --candidate $(CANDIDATE)

explain-dry: ## Print the diagnosis prompt + MCP tool surface; call no model ($0)
	@set -a && . ./.env && set +a && \
	uv run python -m preflight.diagnose --baseline $(BASELINE) --candidate $(CANDIDATE) --dry-run

m6-check: ## M6 acceptance check: force a failure, explain it, prove the explainer is traced
	@set -a && . ./.env && set +a && uv run python scripts/m6_check.py

# Same check, but the investigation is re-run against the live API instead of
# replayed. This is how .cassettes-diagnose/ was recorded. ~$0.06 of Haiku.
m6-check-live: ## M6 check with a fresh, live investigation (costs ~$0.06)
	@set -a && . ./.env && set +a && uv run python scripts/m6_check.py --live
