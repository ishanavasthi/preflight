"""Golden-suite harness: run the cases, flush, and wait for SigNoz to catch up.

The waiting half matters as much as the running half. CI writes traces and then
immediately wants to read aggregates back, with an exporter flush, collector
batching, and a ClickHouse insert in between. Reading too early yields a
partial run and a *silently wrong* diff -- the worst failure mode in this
project. So `wait_for_ingest` polls until the expected span count is visible
and raises on timeout rather than letting the differ proceed.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent import reference
from preflight import otel
from preflight.config import Config
from preflight.instrument import RunContext
from preflight.query import SigNozClient

SUITE_DIR = Path(__file__).resolve().parent.parent / "suite" / "cases"

# Spans emitted per case by the reference agent: 1 case + 1 retrieval + 2 llm
# + one per tool. Used as the target for the ingest poller.
_SPANS_PER_CASE_BASE = 4


class IngestTimeout(RuntimeError):
    pass


@dataclass
class RunOutcome:
    run_id: str
    commit_sha: str
    cases: list[reference.CaseResult]
    expected_spans: int


def load_cases(limit: int | None = None) -> list[dict]:
    paths = sorted(SUITE_DIR.glob("*.yaml"))
    if not paths:
        raise FileNotFoundError(f"no cases found in {SUITE_DIR}")
    cases = [yaml.safe_load(p.read_text()) for p in paths]
    return cases[:limit] if limit else cases


def resolve_commit_sha() -> str:
    """Prefer an explicit override (CI), fall back to git, then to 'unknown'."""
    if sha := os.getenv("PREFLIGHT_COMMIT_SHA"):
        return sha
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def run_suite(cfg: Config, *, limit: int | None = None, run_id: str | None = None) -> RunOutcome:
    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    commit_sha = resolve_commit_sha()

    otel.setup(cfg, commit_sha=commit_sha)
    ctx = RunContext(run_id=run_id, commit_sha=commit_sha)

    cases = load_cases(limit)
    results = [reference.run_case(ctx, cfg, case) for case in cases]

    expected = sum(
        _SPANS_PER_CASE_BASE + len(c.get("tools", ["lookup"])) for c in cases
    )

    otel.force_flush()
    return RunOutcome(
        run_id=run_id, commit_sha=commit_sha, cases=results, expected_spans=expected
    )


def wait_for_ingest(
    cfg: Config, run_id: str, expected_spans: int, *, on_poll=None
) -> int:
    """Block until SigNoz reports `expected_spans` for this run.

    Returns the observed count. Raises IngestTimeout rather than returning a
    partial count -- a loud failure beats a wrong diff.
    """
    deadline = time.monotonic() + cfg.poll_timeout_seconds
    observed = 0
    with SigNozClient(cfg) as client:
        while time.monotonic() < deadline:
            observed = client.span_count(run_id)
            if on_poll:
                on_poll(observed, expected_spans)
            if observed >= expected_spans:
                return observed
            time.sleep(cfg.poll_interval_seconds)

    raise IngestTimeout(
        f"run {run_id}: saw {observed}/{expected_spans} spans in SigNoz after "
        f"{cfg.poll_timeout_seconds}s. Refusing to diff a partially-ingested run. "
        "Raise ingest.poll_timeout_seconds in preflight.yaml if this is expected "
        "for your deployment."
    )
