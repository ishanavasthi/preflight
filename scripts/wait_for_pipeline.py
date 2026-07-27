"""Block until the telemetry pipeline actually persists a span, end to end.

`/api/v1/health` returning 200 means the SigNoz *API* is up. It says nothing
about whether the collector can write to ClickHouse -- and on a cold deployment
it usually can't yet, because the collector fetches its config over opamp
shortly after boot and **restarts**. Anything exported during that window is
accepted by the OTLP endpoint and then silently lost.

That is what broke the first CI run: 0/32 spans, not a partial count. The suite
started 35s before the collector bounced, so the whole run went into the void
while every health check was green.

So this gate proves the thing we actually depend on: write a probe span, then
poll the query API until it comes back. Only then is it safe to run the suite.
Same shape as `runner.wait_for_ingest`, one level lower -- and it fails loudly
rather than letting CI diff a run that was never ingested.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

import httpx

PROBE_SERVICE = "preflight-pipeline-probe"


def _probe_payload(run_id: str) -> dict:
    now_ns = time.time_ns()
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": PROBE_SERVICE}}
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": uuid.uuid4().hex,
                                "spanId": uuid.uuid4().hex[:16],
                                "name": "pipeline-probe",
                                "kind": 1,
                                "startTimeUnixNano": str(now_ns),
                                "endTimeUnixNano": str(now_ns + 1_000_000),
                                "attributes": [
                                    {
                                        "key": "preflight.probe_id",
                                        "value": {"stringValue": run_id},
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def _visible(signoz_url: str, api_key: str, probe_id: str) -> int:
    end_ms = int(time.time() * 1000)
    body = {
        "schemaVersion": "v1",
        "start": end_ms - 15 * 60_000,
        "end": end_ms,
        "requestType": "scalar",
        "compositeQuery": {
            "queries": [
                {
                    "type": "builder_query",
                    "spec": {
                        "name": "A",
                        "signal": "traces",
                        "aggregations": [{"expression": "count()"}],
                        # Qualified: an unqualified name resolves to the
                        # *resource* attribute, not the span attribute.
                        "filter": {
                            "expression": f"attribute.preflight.probe_id = '{probe_id}'"
                        },
                    },
                }
            ]
        },
    }
    resp = httpx.post(
        f"{signoz_url}/api/v5/query_range",
        json=body,
        headers={"SIGNOZ-API-KEY": api_key, "Content-Type": "application/json"},
        timeout=20.0,
    )
    if resp.status_code != 200:
        return 0
    for result in resp.json().get("data", {}).get("data", {}).get("results", []) or []:
        for row in result.get("data", []) or []:
            if isinstance(row, list) and row:
                return int(row[-1] or 0)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeout", type=int, default=300, help="Seconds to keep trying.")
    ap.add_argument("--interval", type=float, default=5.0)
    args = ap.parse_args()

    signoz_url = os.environ.get("SIGNOZ_URL", "http://localhost:8080").rstrip("/")
    otlp = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")
    api_key = os.environ.get("SIGNOZ_API_KEY", "")
    if not api_key:
        print("SIGNOZ_API_KEY is not set", file=sys.stderr)
        return 2

    deadline = time.monotonic() + args.timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        probe_id = f"probe-{uuid.uuid4().hex[:12]}"
        try:
            httpx.post(
                f"{otlp}/v1/traces", json=_probe_payload(probe_id), timeout=15.0
            )
        except httpx.HTTPError as exc:
            print(f"  attempt {attempt}: OTLP endpoint not accepting yet ({exc})")
            time.sleep(args.interval)
            continue

        # Re-probe on each attempt rather than polling one probe forever: if the
        # collector restarts, the earlier probe is gone for good and waiting on
        # it would burn the whole timeout on a span that no longer exists.
        inner = time.monotonic() + 30
        while time.monotonic() < inner and time.monotonic() < deadline:
            try:
                if _visible(signoz_url, api_key, probe_id) > 0:
                    print(f"pipeline ready: probe span queryable on attempt {attempt}")
                    return 0
            except httpx.HTTPError:
                pass
            time.sleep(args.interval)
        print(f"  attempt {attempt}: probe not queryable yet, re-probing")

    print(
        f"FAIL: no probe span became queryable within {args.timeout}s. "
        "The collector accepts OTLP but nothing reaches ClickHouse -- check the "
        "ingester and telemetrystore logs.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
