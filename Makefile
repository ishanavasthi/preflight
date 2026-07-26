SHELL := /bin/bash
.DEFAULT_GOAL := help

# OrbStack ships the docker CLI inside its app bundle and only symlinks it into
# PATH after the GUI first-run completes. Add it unconditionally -- harmless
# when docker is already on PATH.
export PATH := $(HOME)/.local/bin:/Applications/OrbStack.app/Contents/MacOS/xbin:$(PATH)

.PHONY: help up down logs bootstrap install run query check clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

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
