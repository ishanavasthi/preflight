"""preflight CLI.

M1 ships `run` and `query`; M3 adds `diff`, the gate itself. `explain` arrives
in M6.

Exit codes are part of the interface -- M4's GitHub Action branches on them:

    0  compared cleanly, nothing breached
    1  a gated metric breached its threshold (this is the regression signal)
    2  the run could not be ingested in time
    3  SigNoz could not be reached, or there was nothing to compare

1 and 3 are deliberately different. "The baseline run aged out of the lookback
window" must never render in CI as "this PR broke the agent".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from preflight import config as config_mod
from preflight import differ as differ_mod
from preflight import otel, report as report_mod
from preflight.query import Aggregation, SigNozClient, SigNozError
from preflight.runner import IngestTimeout, run_suite, wait_for_ingest


@click.group()
@click.option("--config", "config_path", default=None, help="Path to preflight.yaml.")
@click.pass_context
def main(ctx: click.Context, config_path: str | None) -> None:
    """Catch AI agent regressions in CI, with SigNoz as the source of truth."""
    ctx.ensure_object(dict)
    ctx.obj["cfg"] = config_mod.load(config_path)


@main.command()
@click.option("--cases", "limit", type=int, default=None, help="Run only the first N cases.")
@click.option("--run-id", default=None, help="Override the generated run id.")
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Poll SigNoz until the run's spans are queryable (default: wait).",
)
@click.pass_context
def run(ctx: click.Context, limit: int | None, run_id: str | None, wait: bool) -> None:
    """Run the golden suite and emit one trace per case to SigNoz."""
    cfg = ctx.obj["cfg"]

    outcome = run_suite(cfg, limit=limit, run_id=run_id)
    click.echo(f"run_id      {outcome.run_id}")
    click.echo(f"commit_sha  {outcome.commit_sha}")
    click.echo(f"cases       {len(outcome.cases)}")
    click.echo(f"spans       {outcome.expected_spans} expected")

    if not wait:
        otel.shutdown()
        return

    click.echo("\nwaiting for SigNoz ingest...")

    def progress(observed: int, expected: int) -> None:
        click.echo(f"  {observed}/{expected} spans visible", err=True)

    try:
        observed = wait_for_ingest(
            cfg, outcome.run_id, outcome.expected_spans, on_poll=progress
        )
    except IngestTimeout as exc:
        click.secho(f"\nFAIL: {exc}", fg="red", err=True)
        otel.shutdown()
        sys.exit(2)
    except SigNozError as exc:
        click.secho(f"\nFAIL: {exc}", fg="red", err=True)
        otel.shutdown()
        sys.exit(3)

    click.secho(f"\nOK: {observed} spans queryable in SigNoz.", fg="green")
    click.echo(f"\nNext: preflight query --run-id {outcome.run_id}")
    otel.shutdown()


@main.command()
@click.option("--run-id", required=True, help="The eval.run_id to summarise.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON rows.")
@click.pass_context
def query(ctx: click.Context, run_id: str, as_json: bool) -> None:
    """Read a run back out of SigNoz via /api/v5/query_range."""
    cfg = ctx.obj["cfg"]
    try:
        with SigNozClient(cfg) as client:
            total = client.span_count(run_id)
            rows = client.run_summary(run_id)
    except SigNozError as exc:
        click.secho(f"FAIL: {exc}", fg="red", err=True)
        sys.exit(3)

    if as_json:
        click.echo(json.dumps({"run_id": run_id, "spans": total, "cases": rows}, indent=2))
        return

    click.echo(f"run_id      {run_id}")
    click.echo(f"spans       {total}   (source: SigNoz /api/v5/query_range)")
    if not rows:
        click.secho("no per-case rows returned", fg="yellow")
        return

    click.echo("")
    click.echo(f"{'case':<24}{'spans':>7}{'in_tok':>9}{'out_tok':>9}{'cost_usd':>11}")
    click.echo("-" * 60)
    for row in sorted(rows, key=lambda r: str(r.get("eval.case_id"))):
        click.echo(
            f"{str(row.get('eval.case_id', '-')):<24}"
            f"{int(row.get('spans') or 0):>7}"
            f"{int(row.get('input_tokens') or 0):>9}"
            f"{int(row.get('output_tokens') or 0):>9}"
            f"{float(row.get('cost_usd') or 0):>11.6f}"
        )


@main.command()
@click.option("--baseline", required=True, help="Baseline commit SHA (usually the merge base).")
@click.option("--candidate", required=True, help="Candidate commit SHA (the PR head).")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["markdown", "json"]),
    default="markdown",
    help="markdown for the PR comment, json for a workflow to branch on.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Write the report here instead of stdout.",
)
@click.option(
    "--lookback-minutes",
    type=int,
    default=differ_mod.DEFAULT_LOOKBACK_MINUTES,
    show_default=True,
    help="How far back to search SigNoz for each SHA's most recent run.",
)
@click.pass_context
def diff(
    ctx: click.Context,
    baseline: str,
    candidate: str,
    fmt: str,
    output_path: str | None,
    lookback_minutes: int,
) -> None:
    """Compare two commits' suite runs and gate on the deltas.

    Exits 1 if any gated metric breached its threshold in preflight.yaml.
    """
    cfg = ctx.obj["cfg"]
    try:
        report = differ_mod.diff(
            baseline, candidate, cfg, lookback_minutes=lookback_minutes
        )
    except differ_mod.DiffError as exc:
        click.secho(f"FAIL: {exc}", fg="red", err=True)
        sys.exit(3)
    except SigNozError as exc:
        click.secho(f"FAIL: {exc}", fg="red", err=True)
        sys.exit(3)

    if fmt == "json":
        body = json.dumps(differ_mod.to_dict(report), indent=2)
    else:
        body = report_mod.render_markdown(report, signoz_url=cfg.signoz_url)

    if output_path:
        Path(output_path).write_text(body + "\n")
        click.echo(f"wrote {fmt} report to {output_path}", err=True)
    else:
        click.echo(body)

    summary = differ_mod.breach_summary(report)
    if report.breached:
        click.secho(f"\nREGRESSION: {summary}", fg="red", err=True)
    else:
        click.secho(f"\nOK: {summary}", fg="green", err=True)
    sys.exit(report.exit_code)


@main.command("raw")
@click.option("--filter", "filter_expr", required=True, help="Filter expression.")
@click.option("--agg", "aggs", multiple=True, default=("count()",), help="Aggregation expression.")
@click.option("--group-by", "group_by", multiple=True, help="Attribute to group by.")
@click.option("--minutes", default=60, help="Lookback window.")
@click.pass_context
def raw(ctx, filter_expr, aggs, group_by, minutes):
    """Escape hatch: run an arbitrary scalar query. Useful for debugging M1."""
    import time as _time

    cfg = ctx.obj["cfg"]
    end_ms = int(_time.time() * 1000)
    with SigNozClient(cfg) as client:
        rows = client.scalar(
            aggregations=[Aggregation(a, a.replace("(", "_").replace(")", "").replace(".", "_")) for a in aggs],
            filter_expression=filter_expr,
            group_by=list(group_by),
            start_ms=end_ms - minutes * 60_000,
            end_ms=end_ms,
        )
    click.echo(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
