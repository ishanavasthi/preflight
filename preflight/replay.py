"""Record/replay cache for Anthropic API calls.

The whole project runs on $1 of credit, so the golden suite cannot afford to
hit the API on every run -- and a gate that changes its numbers every time it
executes is not a gate. Both problems have the same fix: record each request
once, key it by a hash of everything that determines the response, and replay
from disk forever after.

Cassettes are **committed on purpose** (`.cassettes/` is deliberately not in
.gitignore). They make the demo deterministic, let CI run the suite for free,
and let the differ attribute a delta to a prompt change rather than to sampling
noise.

Modes:
    - `PREFLIGHT_REPLAY=1`, or no ANTHROPIC_API_KEY  -> replay only. A miss is
      a loud error, never a silent API call.
    - otherwise                                      -> replay on hit, record
      on miss (after a budget check).

Responses are normalised to plain JSON at the boundary, so nothing downstream
depends on the SDK's object model and a cassette stays readable in a diff.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from preflight import budget
from preflight.config import Config

TEMPERATURE = 0.0

CASSETTE_DIR = Path(
    os.getenv("PREFLIGHT_CASSETTES", Path(__file__).resolve().parent.parent / ".cassettes")
)


class ReplayMiss(RuntimeError):
    """No cassette for this request, and we are not allowed to call the API."""


def replay_only() -> bool:
    """True when we must never touch the network.

    A missing key counts: a fresh clone with no credentials should still be
    able to run the whole suite off the committed cassettes.
    """
    if os.getenv("PREFLIGHT_REPLAY", "").strip().lower() in {"1", "true", "yes"}:
        return True
    return not os.getenv("ANTHROPIC_API_KEY")


# --- keying ---------------------------------------------------------------


def request_key(payload: dict[str, Any]) -> str:
    """Stable hash of everything that determines the response.

    Deliberately excludes run_id, commit sha, and timestamps -- two runs of the
    same code must hit the same cassette, or replay is pointless.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def cassette_path(key: str) -> Path:
    return CASSETTE_DIR / f"{key}.json"


def load(key: str) -> dict[str, Any] | None:
    path = cassette_path(key)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save(key: str, payload: dict[str, Any], response: dict[str, Any]) -> None:
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    cassette_path(key).write_text(
        json.dumps(
            {"key": key, "request": payload, "response": response}, indent=2, sort_keys=True
        )
        + "\n"
    )


def count() -> int:
    if not CASSETTE_DIR.exists():
        return 0
    return len(list(CASSETTE_DIR.glob("*.json")))


# --- normalisation --------------------------------------------------------


def _clean(value: Any) -> Any:
    """Drop nulls so a replayed assistant turn is accepted back by the API."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _sleep_recorded_latency(response: dict[str, Any]) -> None:
    """Reproduce the provider latency this response was recorded with.

    Without this, a replayed case finishes in ~0.2ms and `p95_latency_ms`
    becomes sub-millisecond noise -- where an ordinary 0.2ms/0.5ms jitter is a
    +150% swing that trips any sane threshold. That is precisely the "gate is
    flaky, so the team switches it off" failure in BUILD_PLAN's risk register.

    Replaying the recorded wall time makes the latency in the PR comment the
    real agent's latency, and makes it reproducible. Set PREFLIGHT_FAST_REPLAY=1
    to skip the wait when you are iterating on something else and don't care.
    """
    if os.getenv("PREFLIGHT_FAST_REPLAY", "").strip().lower() in {"1", "true", "yes"}:
        return
    latency_ms = response.get("latency_ms")
    if isinstance(latency_ms, (int, float)) and latency_ms > 0:
        time.sleep(min(float(latency_ms), 30_000.0) / 1000.0)


def _normalise(message: Any) -> dict[str, Any]:
    raw = message.model_dump(mode="json")
    return {
        "id": raw.get("id", ""),
        "model": raw.get("model", ""),
        "stop_reason": raw.get("stop_reason"),
        "content": [_clean(block) for block in raw.get("content", [])],
        "usage": {
            "input_tokens": int((raw.get("usage") or {}).get("input_tokens") or 0),
            "output_tokens": int((raw.get("usage") or {}).get("output_tokens") or 0),
        },
    }


# --- the one entry point --------------------------------------------------


def complete(
    cfg: Config,
    *,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int = 256,
) -> tuple[dict[str, Any], bool]:
    """Return `(response, replayed)`.

    On a cassette hit this is free and offline. On a miss it checks the budget
    ledger, calls the API, records the actual cost, and writes the cassette.
    """
    payload = {
        "model": model,
        "system": system,
        "messages": messages,
        "tools": tools,
        "max_tokens": max_tokens,
        # Golden suite: minimise drift when cassettes are re-recorded. It does
        # not make the provider bit-for-bit deterministic, but it keeps a
        # re-record from rewriting every expected answer.
        "temperature": TEMPERATURE,
    }
    key = request_key(payload)

    cached = load(key)
    if cached is not None:
        _sleep_recorded_latency(cached["response"])
        return cached["response"], True

    if replay_only():
        raise ReplayMiss(
            f"no cassette for request {key} and replay mode is on.\n"
            f"  Looked in: {cassette_path(key)}\n"
            "  Either the prompt/tools/model changed (re-record with "
            "ANTHROPIC_API_KEY set and PREFLIGHT_REPLAY unset), or the "
            "cassettes were not committed."
        )

    # Rough pre-flight estimate: the ledger has to be checked *before* the
    # call, so err high -- input chars/4 plus the full max_tokens of output.
    price = cfg.price(model)
    est_input = len(json.dumps(payload, default=str)) // 4 + 128
    estimated = price.cost_usd(est_input, max_tokens)
    budget.check(estimated)

    import anthropic  # imported lazily: replay-only runs need no SDK auth

    client = anthropic.Anthropic()
    call_started = time.perf_counter()
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        tools=tools,
        temperature=TEMPERATURE,
    )
    response = _normalise(message)
    response["latency_ms"] = round((time.perf_counter() - call_started) * 1000, 3)

    actual = price.cost_usd(
        response["usage"]["input_tokens"], response["usage"]["output_tokens"]
    )
    budget.record(actual)

    save(key, payload, response)
    return response, False
