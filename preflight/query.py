"""SigNoz query API client.

This module is the load-bearing bet of the whole project: SigNoz is the
datastore for the gate, not a dashboard bolted on the side. Every number the
differ reports comes back through `/api/v5/query_range` -- there is no local
results file to fall back on.

API shape (verified against SigNoz v0.134.0):
    POST {url}/api/v5/query_range
    Header: SIGNOZ-API-KEY: {key}
    Body:  {start, end, requestType, compositeQuery: {queries: [...]}}
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from preflight.config import Config
from preflight.contracts import CaseSummary, RunSummary


class SigNozError(RuntimeError):
    pass


@dataclass
class Aggregation:
    """One aggregation column, e.g. `sum(gen_ai.usage.input_tokens)`."""

    expression: str
    alias: str


class SigNozClient:
    def __init__(self, cfg: Config, *, timeout: float = 30.0):
        if not cfg.signoz_api_key:
            raise SigNozError(
                "SIGNOZ_API_KEY is not set. Mint one with `make signoz-bootstrap` "
                "or from Settings -> Service Accounts in the SigNoz UI."
            )
        self.cfg = cfg
        self._client = httpx.Client(
            base_url=cfg.signoz_url,
            headers={
                "SIGNOZ-API-KEY": cfg.signoz_api_key,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SigNozClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- raw ---------------------------------------------------------------

    def query_range(self, body: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post("/api/v5/query_range", json=body)
        if resp.status_code != 200:
            raise SigNozError(
                f"query_range returned HTTP {resp.status_code}: {resp.text[:600]}"
            )
        payload = resp.json()
        if payload.get("status") != "success":
            raise SigNozError(f"query_range failed: {payload}")
        return payload

    # -- builder -----------------------------------------------------------

    def scalar(
        self,
        *,
        aggregations: list[Aggregation],
        filter_expression: str,
        group_by: list[str | tuple[str, str]] | None = None,
        start_ms: int,
        end_ms: int,
        signal: str = "traces",
    ) -> list[dict[str, Any]]:
        """Run a scalar (non-time-series) builder query and flatten the rows.

        `group_by` entries are span attributes by default -- which is exactly
        the question Unknown #1 in BUILD_PLAN.md asks. Pass a
        `(name, field_context)` tuple to group by an intrinsic span field such
        as `trace_id`, which lives in the `span` context rather than
        `attribute`. Returns a list of row dicts mapping every group-by key and
        every aggregation alias to its value.
        """
        body = {
            "schemaVersion": "v1",
            "start": start_ms,
            "end": end_ms,
            "requestType": "scalar",
            "compositeQuery": {
                "queries": [
                    {
                        "type": "builder_query",
                        "spec": {
                            "name": "A",
                            "signal": signal,
                            "disabled": False,
                            "aggregations": [
                                {"expression": a.expression, "alias": a.alias}
                                for a in aggregations
                            ],
                            "filter": {"expression": filter_expression},
                            "groupBy": [
                                {"name": g, "fieldContext": "attribute"}
                                if isinstance(g, str)
                                else {"name": g[0], "fieldContext": g[1]}
                                for g in (group_by or [])
                            ],
                        },
                    }
                ]
            },
            "formatOptions": {"formatTableResultForUI": False, "fillGaps": False},
        }
        return _flatten_scalar(
            self.query_range(body), aliases=[a.alias for a in aggregations]
        )

    # -- the pieces M1 needs ----------------------------------------------

    def span_count(self, run_id: str, *, lookback_minutes: int = 60) -> int:
        """Total spans SigNoz can currently see for one eval run."""
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - lookback_minutes * 60_000
        rows = self.scalar(
            aggregations=[Aggregation("count()", "span_count")],
            filter_expression=f"attribute.eval.run_id = '{run_id}'",
            start_ms=start_ms,
            end_ms=end_ms,
        )
        if not rows:
            return 0
        return int(rows[0].get("span_count") or 0)

    def run_summary(
        self, run_id: str, *, lookback_minutes: int = 60
    ) -> list[dict[str, Any]]:
        """Per-case aggregates for one run, grouped by `eval.case_id`."""
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - lookback_minutes * 60_000
        return self.scalar(
            aggregations=[
                Aggregation("count()", "spans"),
                Aggregation("sum(gen_ai.usage.input_tokens)", "input_tokens"),
                Aggregation("sum(gen_ai.usage.output_tokens)", "output_tokens"),
                Aggregation("sum(preflight.cost_usd)", "cost_usd"),
            ],
            filter_expression=f"attribute.eval.run_id = '{run_id}'",
            group_by=["eval.case_id"],
            start_ms=start_ms,
            end_ms=end_ms,
        )

    # -- the M3 seam -------------------------------------------------------

    def run_summary_typed(
        self, run_id: str, *, lookback_minutes: int = 60
    ) -> RunSummary:
        """One whole run, as `contracts.RunSummary`. This is what M3 consumes.

        `run_summary()` above returns raw rows and stays as-is for the CLI and
        for debugging; this returns the pinned dataclass with every field the
        gate needs populated, so the differ never has to know what a SigNoz row
        looks like.

        Four queries rather than one, because SigNoz aggregates over the rows a
        filter selects and the counts we need come from *different* subsets of
        spans: tool spans, retrieval spans, and everything. Conditional
        aggregation would fold that into one round trip, but four small scalar
        queries are ~100ms total and are obviously correct, which matters more
        for the number that fails someone's PR.
        """
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - lookback_minutes * 60_000
        # Qualify with `attribute.` explicitly. An unqualified name in a
        # SigNoz filter resolves against *resource* attributes first, and
        # otel.py stamps vcs.commit_sha on the resource -- so an unqualified
        # filter silently means something different from what it reads like.
        # eval.run_id happens to exist only as a span attribute today, which
        # makes the unqualified form work by luck rather than by design.
        where = f"attribute.eval.run_id = '{run_id}'"

        totals = self.scalar(
            aggregations=[
                Aggregation("count()", "spans"),
                Aggregation("sum(gen_ai.usage.input_tokens)", "input_tokens"),
                Aggregation("sum(gen_ai.usage.output_tokens)", "output_tokens"),
                Aggregation("sum(preflight.cost_usd)", "cost_usd"),
                Aggregation("sum(preflight.duration_ms)", "duration_ms"),
                Aggregation("sum(preflight.success)", "success"),
            ],
            filter_expression=where,
            group_by=["eval.case_id", "vcs.commit_sha"],
            start_ms=start_ms,
            end_ms=end_ms,
        )

        def _role_counts(role: str, alias: str) -> dict[str, int]:
            rows = self.scalar(
                aggregations=[Aggregation("count()", alias)],
                filter_expression=f"{where} AND attribute.preflight.span_role = '{role}'",
                group_by=["eval.case_id"],
                start_ms=start_ms,
                end_ms=end_ms,
            )
            return {
                str(r.get("eval.case_id", "")): int(r.get(alias) or 0) for r in rows
            }

        tool_calls = _role_counts("tool", "tool_calls")
        retrieval_hops = _role_counts("retrieval", "retrieval_hops")
        trace_ids = self._trace_ids(run_id, start_ms=start_ms, end_ms=end_ms)

        commit_sha = ""
        cases: list[CaseSummary] = []
        for row in totals:
            case_id = str(row.get("eval.case_id", ""))
            if not case_id:
                continue
            commit_sha = commit_sha or str(row.get("vcs.commit_sha", "") or "")
            cases.append(
                CaseSummary(
                    case_id=case_id,
                    spans=int(row.get("spans") or 0),
                    input_tokens=int(row.get("input_tokens") or 0),
                    output_tokens=int(row.get("output_tokens") or 0),
                    cost_usd=float(row.get("cost_usd") or 0.0),
                    duration_ms=float(row.get("duration_ms") or 0.0),
                    tool_calls=tool_calls.get(case_id, 0),
                    retrieval_hops=retrieval_hops.get(case_id, 0),
                    # `preflight.success` is 1/0 on the single case span, so the
                    # group sum is the case's own verdict.
                    success=float(row.get("success") or 0.0) >= 1.0,
                    trace_id=trace_ids.get(case_id, ""),
                )
            )

        cases.sort(key=lambda c: c.case_id)
        return RunSummary(run_id=run_id, commit_sha=commit_sha, cases=cases)

    def _trace_ids(
        self, run_id: str, *, start_ms: int, end_ms: int
    ) -> dict[str, str]:
        """Map case_id -> trace_id, so the PR comment can deep-link the trace.

        `trace_id` is an intrinsic span field, not an attribute, so it needs
        `fieldContext: "span"`. Best-effort: a missing deep link degrades the
        report, a raised exception would fail the gate for a cosmetic reason.
        """
        try:
            rows = self.scalar(
                aggregations=[Aggregation("count()", "spans")],
                filter_expression=(
                    f"attribute.eval.run_id = '{run_id}' "
                    "AND attribute.preflight.span_role = 'case'"
                ),
                group_by=["eval.case_id", ("trace_id", "span")],
                start_ms=start_ms,
                end_ms=end_ms,
            )
        except SigNozError:
            return {}
        return {
            str(r.get("eval.case_id", "")): str(r.get("trace_id", "") or "")
            for r in rows
            if r.get("eval.case_id")
        }


def _flatten_scalar(
    payload: dict[str, Any], *, aliases: list[str] | None = None
) -> list[dict[str, Any]]:
    """Turn a v5 scalar response into plain row dicts.

    Shape, verified against SigNoz v0.134.0:

        data.data.results[] -> {columns: [...], data: [[...], ...]}

    Each row is a list positionally matching `columns`. Group-by columns carry
    `columnType: "group"` and are named after the attribute; aggregation columns
    carry `columnType: "aggregation"` and are named `__result_0`, `__result_1`,
    ... in the order the aggregations were requested -- the `alias` we send is
    *not* echoed back. So we re-attach our aliases by `aggregationIndex`, and
    callers get to keep using readable names.
    """
    aliases = aliases or []
    rows: list[dict[str, Any]] = []

    for result in payload.get("data", {}).get("data", {}).get("results", []) or []:
        raw_columns = result.get("columns", []) or []
        names: list[str] = []
        agg_seen = 0
        for col in raw_columns:
            name = col.get("name", "")
            if col.get("columnType") == "aggregation":
                # Prefer our requested alias; fall back to the server's name.
                idx = col.get("aggregationIndex", agg_seen)
                names.append(aliases[idx] if idx < len(aliases) else name)
                agg_seen += 1
            else:
                names.append(name)

        for raw_row in result.get("data", []) or []:
            if isinstance(raw_row, dict):
                rows.append(raw_row)
            else:
                rows.append(dict(zip(names, raw_row)))
    return rows
