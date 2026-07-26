SHELL := /bin/bash
.DEFAULT_GOAL := help

# OrbStack ships the docker CLI inside its app bundle and only symlinks it into
# PATH after the GUI first-run completes. Add it unconditionally -- harmless
# when docker is already on PATH.
export PATH := $(HOME)/.local/bin:/Applications/OrbStack.app/Contents/MacOS/xbin:$(PATH)

.PHONY: help up down logs bootstrap install run query check clean \
        report-sample verify-links lint-ci ci-local

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
