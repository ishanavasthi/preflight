"""Apply the committed dashboards and alert rules to SigNoz, idempotently.

Everything here goes through the **SigNoz MCP server**, not the REST API.
BUILD_PLAN M5 recommends it and the recommendation held up: `signoz_create_dashboard`
and `signoz_create_alert` take the payloads verbatim, so `dashboards/*.json` and
`alerts/*.json` are the literal tool arguments rather than a shape this script has
to translate into a REST body. See DECISIONS.md for what that cost and bought.

Idempotency is **client-side, and it has to be.** SigNoz does not treat a
dashboard's `name` as a unique key -- creating the same payload twice yields two
dashboards with the same `name` and different UUIDs (verified). So every run
lists what exists, matches on the stable identity field, and updates in place:

    dashboards   matched on the top-level `name` slug   (e.g. preflight-agent-health)
    alert rules  matched on the `alert` title           (e.g. "Preflight - agent ...")

A stale duplicate -- a second copy created by an earlier non-idempotent run, or by
hand in the UI -- is deleted rather than left to drift, so `make signoz-apply`
converges the deployment to exactly what is in git.

Usage
-----
    python scripts/signoz_apply.py            # apply dashboards + alerts
    python scripts/signoz_apply.py --verify   # run every panel query, report row counts
    python scripts/signoz_apply.py --dry-run  # say what would change, change nothing
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.mcp_client import MCPClient, MCPError  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "dashboards"
ALERT_DIR = ROOT / "alerts"

# The alert rules route here. Created if absent -- an alert referencing an unknown
# channel is rejected outright, so this is a prerequisite, not a nicety.
CHANNEL_NAME = "preflight-local-webhook"
CHANNEL_SPEC = {
    "type": "webhook",
    "name": CHANNEL_NAME,
    # Nothing listens on this port; the rule still evaluates and fires, it just
    # cannot deliver. A real deployment points this at Slack or PagerDuty.
    "webhook_url": "http://host.docker.internal:9099/preflight-alerts",
    "send_resolved": True,
}

CONTEXT = "make signoz-apply: reconcile committed preflight dashboards and alert rules"


# --- helpers ---------------------------------------------------------------


def _load(directory: pathlib.Path) -> list[tuple[pathlib.Path, dict[str, Any]]]:
    return [(p, json.loads(p.read_text())) for p in sorted(directory.glob("*.json"))]


def _unwrap(payload: Any) -> Any:
    """SigNoz MCP wraps most results in {"status": ..., "data": ...}."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _builder_specs(panel: dict[str, Any]) -> list[dict[str, Any]]:
    """Every builder query a panel runs, formula panels included.

    A plain panel holds one `signoz/BuilderQuery`. A formula panel holds one
    `signoz/CompositeQuery` whose `spec.queries[]` carries the input builder
    queries plus a `builder_formula` envelope that combines them -- the formula
    itself has no filter to test, so only the inputs are returned.
    """
    specs: list[dict[str, Any]] = []
    for query in panel["spec"]["queries"]:
        # The panel declares its own request type ("time_series" for charts,
        # "scalar" for tables). Reusing it here means the check issues the same
        # request the UI does, rather than a scalar stand-in that could pass
        # while the rendered panel stays blank.
        request_type = query.get("kind", "scalar")
        plugin = query["spec"]["plugin"]
        if plugin["kind"] == "signoz/BuilderQuery":
            specs.append((request_type, plugin["spec"]))
        elif plugin["kind"] == "signoz/CompositeQuery":
            specs.extend(
                (request_type, env["spec"])
                for env in plugin["spec"].get("queries", [])
                if env.get("type") == "builder_query"
            )
    return specs


def _count_rows(payload: dict[str, Any], request_type: str) -> int:
    """Rows for a scalar response, series for a time_series one.

    The two shapes differ and neither is nested where the other is: scalar rows
    sit at `results[].data`, while time_series lands at
    `results[].aggregations[].series[]`. Counting the wrong one reads as zero.
    """
    results = payload["data"]["data"]["results"]
    if request_type == "time_series":
        return sum(
            len(agg.get("series") or [])
            for r in results
            for agg in (r.get("aggregations") or [])
        )
    return sum(len(r.get("data") or []) for r in results)


class Reconciler:
    def __init__(self, mcp: MCPClient, *, dry_run: bool = False):
        self.mcp = mcp
        self.dry_run = dry_run
        self.changes: list[str] = []

    def _say(self, line: str) -> None:
        print(line)
        self.changes.append(line)

    # -- notification channel ---------------------------------------------

    def ensure_channel(self) -> None:
        existing = _unwrap(
            self.mcp.call("signoz_list_notification_channels", {"searchContext": CONTEXT})
        )
        names = {c.get("name") for c in (existing or [])}
        if CHANNEL_NAME in names:
            print(f"  channel  {CHANNEL_NAME}: ok")
            return
        if self.dry_run:
            self._say(f"  channel  {CHANNEL_NAME}: WOULD CREATE")
            return
        self.mcp.call(
            "signoz_create_notification_channel", {**CHANNEL_SPEC, "searchContext": CONTEXT}
        )
        # The server test-pings the webhook on create and reports failure in the
        # result; that is expected here (nothing listens) and is not fatal.
        self._say(f"  channel  {CHANNEL_NAME}: created")

    # -- dashboards --------------------------------------------------------

    def _remote_dashboards(self) -> dict[str, list[dict[str, Any]]]:
        """Map name slug -> [summary, ...]. A list, because names are not unique."""
        out: dict[str, list[dict[str, Any]]] = {}
        offset = 0
        while True:
            page = _unwrap(
                self.mcp.call(
                    "signoz_list_dashboards",
                    {"searchContext": CONTEXT, "limit": 200, "offset": offset},
                )
            )
            for d in page.get("dashboards", []) or []:
                out.setdefault(d.get("name", ""), []).append(d)
            total = page.get("total", 0)
            offset += 200
            if offset >= total:
                break
        return out

    def apply_dashboards(self) -> None:
        remote = self._remote_dashboards()
        for path, spec in _load(DASHBOARD_DIR):
            name = spec["name"]
            matches = remote.get(name, [])
            payload = {**spec, "searchContext": CONTEXT}

            if not matches:
                if self.dry_run:
                    self._say(f"  {path.name}: WOULD CREATE ({name})")
                    continue
                created = _unwrap(self.mcp.call("signoz_create_dashboard", payload))
                self._say(f"  {path.name}: created  {created['id']}")
                continue

            keep, *dupes = sorted(matches, key=lambda d: d.get("createdAt", ""))
            if self.dry_run:
                self._say(f"  {path.name}: WOULD UPDATE {keep['id']}")
            else:
                self.mcp.call("signoz_update_dashboard", {**payload, "id": keep["id"]})
                self._say(f"  {path.name}: updated  {keep['id']}")
            for d in dupes:
                if self.dry_run:
                    self._say(f"  {path.name}: WOULD DELETE duplicate {d['id']}")
                else:
                    self.mcp.call(
                        "signoz_delete_dashboard", {"searchContext": CONTEXT, "id": d["id"]}
                    )
                    self._say(f"  {path.name}: deleted duplicate {d['id']}")

    # -- alert rules -------------------------------------------------------

    def _remote_alerts(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        rules = _unwrap(
            self.mcp.call("signoz_list_alert_rules", {"searchContext": CONTEXT, "limit": 200})
        )
        if isinstance(rules, dict):
            rules = rules.get("rules") or rules.get("data") or []
        for r in rules or []:
            title = r.get("alert") or r.get("name") or ""
            out.setdefault(title, []).append(r)
        return out

    @staticmethod
    def _rule_id(rule: dict[str, Any]) -> str | None:
        """Alert-rule identity.

        `signoz_list_alert_rules` returns the UUID as **`ruleId`**, while
        `signoz_create_alert` returns it as `id` and `signoz_update_alert` expects
        `id`. Reading only `id` off a listing yields None, and the update then
        silently fails to target anything -- so accept both.
        """
        return rule.get("id") or rule.get("ruleId")

    def apply_alerts(self) -> None:
        remote = self._remote_alerts()
        for path, spec in _load(ALERT_DIR):
            title = spec["alert"]
            matches = remote.get(title, [])
            payload = {**spec, "searchContext": CONTEXT}

            if not matches:
                if self.dry_run:
                    self._say(f"  {path.name}: WOULD CREATE ({title})")
                    continue
                created = _unwrap(self.mcp.call("signoz_create_alert", payload))
                rid = created.get("id") if isinstance(created, dict) else created
                self._say(f"  {path.name}: created  {rid}")
                continue

            keep, *dupes = sorted(matches, key=lambda r: str(r.get("createdAt", "")))
            rid = self._rule_id(keep)
            if rid is None:
                raise MCPError(
                    f"{path.name}: alert {title!r} exists remotely but carries no id "
                    "-- refusing to create a duplicate"
                )
            if self.dry_run:
                self._say(f"  {path.name}: WOULD UPDATE {rid}")
            else:
                self.mcp.call("signoz_update_alert", {**payload, "id": rid})
                self._say(f"  {path.name}: updated  {rid}")
            for r in dupes:
                did = self._rule_id(r)
                if self.dry_run:
                    self._say(f"  {path.name}: WOULD DELETE duplicate {did}")
                else:
                    self.mcp.call(
                        "signoz_delete_alert", {"searchContext": CONTEXT, "id": did}
                    )
                    self._say(f"  {path.name}: deleted duplicate {did}")


# --- verification ----------------------------------------------------------


# Panels that are *expected* to be empty on a healthy deployment. Listed
# explicitly, with a reason, so that "no rows" stays a failure everywhere else
# rather than becoming a shrug. An empty panel and a broken panel look identical
# in the UI, which is the whole reason this check exists.
ALLOW_EMPTY = {
    ("tool-trajectory.json", "tool_errors"): (
        "no tool span has ever errored -- a healthy suite leaves this empty, and "
        "it is the series the tool-error-rate alert watches"
    ),
}


def verify(lookback_minutes: int = 60) -> int:
    """Execute every committed panel query and report how many rows it returns.

    A dashboard that renders is not a dashboard that *works* -- an empty panel
    looks identical to a healthy one that happens to be quiet. So this runs each
    panel's builder query through the same `/api/v5/query_range` the CI gate uses
    and fails if any panel comes back with no rows.

    Formula panels are unwrapped, not skipped: a `signoz/CompositeQuery` plugin
    carries its input builder queries in `spec.queries[]`, and each one is checked
    on its own. A formula has no filter of its own, so proving A and B return rows
    proves F1 can be computed.
    """
    from preflight import config
    from preflight.query import SigNozClient, SigNozError

    cfg = config.load()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - lookback_minutes * 60_000
    failures = 0
    checked = 0

    with SigNozClient(cfg) as client:
        for path, spec in _load(DASHBOARD_DIR):
            print(f"\n{path.name}  ({spec['spec']['display']['name']})")
            for key, panel in spec["spec"]["panels"].items():
                for request_type, qs in _builder_specs(panel):
                    body = {
                        "schemaVersion": "v1",
                        "start": start_ms,
                        "end": end_ms,
                        "requestType": request_type,
                        "compositeQuery": {
                            "queries": [
                                {
                                    "type": "builder_query",
                                    "spec": {
                                        "name": "A",
                                        "signal": qs["signal"],
                                        "aggregations": [
                                            {"expression": a["expression"]}
                                            for a in qs["aggregations"]
                                        ],
                                        "filter": qs["filter"],
                                        "groupBy": qs.get("groupBy", []),
                                        **({"stepInterval": qs["stepInterval"]}
                                           if request_type == "time_series"
                                           and qs.get("stepInterval") else {}),
                                    },
                                }
                            ]
                        },
                    }
                    checked += 1
                    label = f"  {key}.{qs['name']} [{request_type[:4]}]"
                    try:
                        rows = _count_rows(client.query_range(body), request_type)
                    except SigNozError as exc:
                        print(f"{label:44} ERROR  {str(exc)[:90]}")
                        failures += 1
                        continue
                    if rows:
                        print(f"{label:44} ok    rows={rows}")
                    elif (path.name, key) in ALLOW_EMPTY:
                        print(f"{label:44} empty rows=0  (expected: "
                              f"{ALLOW_EMPTY[(path.name, key)]})")
                    else:
                        print(f"{label:44} EMPTY rows=0")
                        failures += 1

    print(f"\n{checked - failures}/{checked} panel queries returned data "
          f"(lookback {lookback_minutes}m)")
    return 1 if failures else 0


# --- entrypoint ------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report changes, make none")
    ap.add_argument("--verify", action="store_true",
                    help="execute every panel query and report row counts")
    ap.add_argument("--lookback", type=int, default=60,
                    help="minutes of history --verify queries over (default 60)")
    args = ap.parse_args()

    if args.verify:
        return verify(args.lookback)

    try:
        with MCPClient() as mcp:
            print(f"SigNoz MCP  {mcp.url}")
            rec = Reconciler(mcp, dry_run=args.dry_run)
            print("notification channels:")
            rec.ensure_channel()
            print("dashboards:")
            rec.apply_dashboards()
            print("alert rules:")
            rec.apply_alerts()
    except MCPError as exc:
        print(f"\nMCP error: {exc}", file=sys.stderr)
        return 1

    print(f"\n{len(rec.changes)} object(s) reconciled"
          f"{' (dry run -- nothing changed)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
