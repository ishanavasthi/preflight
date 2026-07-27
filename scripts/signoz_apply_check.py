"""BUILD_PLAN M5 acceptance check, automated.

    "Delete a dashboard from the UI, run `make signoz-apply`, it comes back identical."

Doing that by hand proves it once. This proves it on demand, and proves the
stronger claim: not merely that *a* dashboard reappears, but that the definition
which comes back is byte-identical to the one that was destroyed. That is the
difference between "apply recreated something" and "the committed JSON is the
source of truth".

Method:

1. Apply, then snapshot the live definition of every preflight dashboard.
2. Delete one outright through MCP -- the same call the UI's delete button makes
   -- and confirm SigNoz really lost it.
3. Re-apply from the committed files.
4. Diff the new live definition against the snapshot, ignoring only the fields
   the server owns and must change: the UUID, and the created/updated timestamps.
   Everything else -- every panel, query, filter, layout coordinate and unit --
   has to match exactly.
5. Apply a third time and assert nothing is created, which is the idempotency
   half: a second run must converge, not accumulate duplicates.
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.mcp_client import MCPClient  # noqa: E402
from scripts.signoz_apply import CONTEXT, Reconciler, _load, DASHBOARD_DIR  # noqa: E402

# Server-owned. A new UUID and fresh timestamps are correct behaviour after a
# delete; anything else differing is a bug in the apply path.
VOLATILE = {"id", "uuid", "createdAt", "updatedAt", "createdBy", "updatedBy", "webUrl"}

# The dashboard sacrificed to the check.
TARGET = "preflight-tool-trajectory"


def _strip(dashboard: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(dashboard)
    for key in VOLATILE:
        out.pop(key, None)
    return out


def _live(mcp: MCPClient) -> dict[str, dict[str, Any]]:
    """name slug -> full live definition, for the committed dashboards only."""
    wanted = {spec["name"] for _, spec in _load(DASHBOARD_DIR)}
    listing = mcp.call(
        "signoz_list_dashboards", {"searchContext": CONTEXT, "limit": 200}
    )["data"]
    out = {}
    for summary in listing.get("dashboards", []) or []:
        if summary.get("name") in wanted:
            full = mcp.call(
                "signoz_get_dashboard", {"searchContext": CONTEXT, "id": summary["id"]}
            )["data"]
            out[full["name"]] = full
    return out


def main() -> int:
    with MCPClient() as mcp:
        print("=" * 72)
        print("M5 acceptance check: delete a dashboard, re-apply, prove it is identical")
        print("=" * 72)

        print("\n[1/5] apply, then snapshot the live definitions")
        Reconciler(mcp).apply_dashboards()
        before = _live(mcp)
        if TARGET not in before:
            print(f"FAIL: {TARGET} was not applied")
            return 1
        for name, d in sorted(before.items()):
            panels = len(d["spec"]["panels"])
            print(f"       {name:32} id={d['id']}  panels={panels}")

        victim = before[TARGET]
        print(f"\n[2/5] delete {TARGET} (id={victim['id']})")
        mcp.call("signoz_delete_dashboard", {"searchContext": CONTEXT, "id": victim["id"]})
        gone = _live(mcp)
        if TARGET in gone:
            print("FAIL: dashboard still present after delete")
            return 1
        print(f"       confirmed gone -- {len(gone)}/{len(before)} preflight dashboards remain")

        print("\n[3/5] make signoz-apply (re-apply from the committed JSON)")
        Reconciler(mcp).apply_dashboards()

        print("\n[4/5] diff the restored definition against the snapshot")
        after = _live(mcp)
        if TARGET not in after:
            print("FAIL: dashboard did not come back")
            return 1
        restored = after[TARGET]
        a, b = _strip(victim), _strip(restored)
        if a != b:
            print("FAIL: restored dashboard differs from the original")
            for line in _diff(a, b):
                print(f"       {line}")
            return 1
        print(f"       identical: {len(json.dumps(b))} bytes of definition match exactly")
        print(f"       (new id {restored['id']} -- a fresh UUID is expected and ignored)")

        print("\n[5/5] idempotency: apply again, assert nothing is created")
        rec = Reconciler(mcp)
        rec.apply_dashboards()
        created = [c for c in rec.changes if "created" in c]
        if created:
            print("FAIL: a second apply created objects instead of converging")
            for c in created:
                print(f"       {c}")
            return 1
        print("       converged -- every dashboard updated in place, none created")

    print("\n" + "=" * 72)
    print("PASS: the committed JSON is the source of truth for SigNoz")
    print("=" * 72)
    return 0


def _diff(a: Any, b: Any, path: str = "") -> list[str]:
    """Minimal structural diff, for when the check fails and you need to know why."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: only in restored")
            elif k not in b:
                out.append(f"{path}.{k}: only in original")
            else:
                out.extend(_diff(a[k], b[k], f"{path}.{k}"))
        return out
    if a != b:
        return [f"{path}: {str(a)[:80]!r} != {str(b)[:80]!r}"]
    return []


if __name__ == "__main__":
    raise SystemExit(main())
