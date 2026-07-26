"""The reference agent under test.

M1 scope: this is deliberately a stub. Its job is to prove the telemetry
round-trip -- emit spans, get them into SigNoz, read them back -- not to be a
good agent. Token counts are derived deterministically from the case so that
two runs of the same commit produce identical numbers and any delta the differ
reports is a real signal rather than sampling noise.

M2 replaces `_fake_llm_call` with a real claude-sonnet-5 call and grows this
into a multi-tool agent with a retrieval hop.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from preflight import instrument
from preflight.config import Config
from preflight.instrument import RunContext

M1_MODEL = "fake-model-m1"


@dataclass
class CaseResult:
    case_id: str
    answer: str
    input_tokens: int
    output_tokens: int
    tool_calls: list[str]
    success: bool


def _deterministic_tokens(seed: str) -> tuple[int, int]:
    """Stable pseudo-token counts so a run is reproducible for a given case."""
    digest = hashlib.sha256(seed.encode()).digest()
    return 400 + digest[0] * 2, 80 + digest[1]


def _fake_llm_call(ctx: RunContext, cfg: Config, *, seed: str, prompt: str) -> str:
    """Stand-in for a real provider call. Replaced with the SDK in M2."""
    with instrument.llm_span(ctx, cfg, model=M1_MODEL, provider="anthropic") as result:
        # A little latency so p95 numbers are not all zero.
        time.sleep(0.05)
        result.input_tokens, result.output_tokens = _deterministic_tokens(seed)
        return f"answer to {prompt[:40]}"


def run_case(ctx: RunContext, cfg: Config, case: dict) -> CaseResult:
    """Execute one golden-suite case, fully instrumented."""
    case_id = case["id"]
    prompt = case.get("prompt", "")

    with instrument.case_span(ctx, case_id):
        with instrument.retrieval_span(ctx, name="kb", query=prompt):
            time.sleep(0.02)

        plan = _fake_llm_call(ctx, cfg, seed=f"{case_id}:plan", prompt=prompt)

        tool_calls: list[str] = []
        for i, tool in enumerate(case.get("tools", ["lookup"])):
            with instrument.tool_span(ctx, name=tool, call_id=f"{case_id}-{i}"):
                time.sleep(0.01)
            tool_calls.append(tool)

        answer = _fake_llm_call(ctx, cfg, seed=f"{case_id}:answer", prompt=plan)

    in_tok, out_tok = _deterministic_tokens(f"{case_id}:plan")
    in2, out2 = _deterministic_tokens(f"{case_id}:answer")
    return CaseResult(
        case_id=case_id,
        answer=answer,
        input_tokens=in_tok + in2,
        output_tokens=out_tok + out2,
        tool_calls=tool_calls,
        success=True,
    )
