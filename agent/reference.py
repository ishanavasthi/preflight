"""The reference agent under test.

A real tool-calling loop against Claude Haiku 4.5 over the fake dataset in
`agent/data.py`: one grounding retrieval hop, then up to two model turns with
three tools available. Five to eight spans per case, all carrying the GenAI
semantic-convention attributes the gate queries by name.

Two things about how this is wired are deliberate:

**Every model call goes through `preflight.replay`.** The project has $1 of API
credit and the gate has to produce the same numbers twice, so responses are
recorded once and replayed from `.cassettes/` forever after. Replay is the
default execution path; the API is only touched on a cassette miss with a key
present, and even then only after `preflight.budget.check()` allows it.

**The M1 stub is still here, behind `PREFLIGHT_AGENT=stub`.** It needs no
credentials and no cassettes, which keeps the telemetry plumbing testable in
isolation from the model.

`SYSTEM_PROMPT` is the seam the seeded regression branch edits -- one line,
which is exactly the kind of change this whole project exists to catch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field

from agent import data
from preflight import instrument, replay
from preflight.config import Config
from preflight.instrument import RunContext

log = logging.getLogger("preflight.agent")

MODEL = "claude-haiku-4-5-20251001"
M1_MODEL = "fake-model-m1"
MAX_TOKENS = 256
# Harness ceiling, not a target. The baseline agent finishes every case in two
# calls and never approaches this; the headroom exists so a regression can
# actually *show* a longer trajectory instead of being silently clipped to look
# identical to baseline. A gate that caps the thing it measures cannot see the
# thing it is measuring -- with the cap at 3, the seeded regression came out at
# +16% tokens, the "15%, not 3x" case BUILD_PLAN's risk register warns against.
# Worst case is 5 calls x 6 cases on a re-record, which the budget absorbs.
MAX_MODEL_TURNS = 5

# --- The one line the seeded regression edits -----------------------------
SYSTEM_PROMPT = (
    "You are a support agent for Northwind Outfitters. "
    "Work strictly one step at a time and call exactly one tool per step, "
    "never more than one. First call policy_search for the general rule. Then "
    "call lookup_order for the order. Then call check_inventory for the SKU "
    "you found on that order. Then call policy_search once more for the "
    "specific rule that applies to that item's status. Only after all four "
    "steps, enumerate every option available to the customer with its "
    "trade-offs before giving your recommendation."
)

TOOLS: list[dict] = [
    {
        "name": "lookup_order",
        "description": "Look up an order by its numeric id. Returns status, ship age, items and total.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "e.g. 8841"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "policy_search",
        "description": "Search the store policy knowledge base. Use for refunds, returns, exchanges, shipping and backorder rules.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look up."}},
            "required": ["query"],
        },
    },
    {
        "name": "check_inventory",
        "description": "Check stock for a SKU. Returns on_hand count and whether it is backordered.",
        "input_schema": {
            "type": "object",
            "properties": {"sku": {"type": "string", "description": "e.g. TENT-2P"}},
            "required": ["sku"],
        },
    },
]


@dataclass
class CaseResult:
    case_id: str
    answer: str
    input_tokens: int
    output_tokens: int
    tool_calls: list[str]
    success: bool
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    retrieval_hops: int = 0
    spans: int = 0
    replayed: bool = True
    trace_id: str = ""
    transcript: list[str] = field(default_factory=list)


def use_stub() -> bool:
    return os.getenv("PREFLIGHT_AGENT", "").strip().lower() == "stub"


def run_case(ctx: RunContext, cfg: Config, case: dict) -> CaseResult:
    if use_stub():
        return _run_case_stub(ctx, cfg, case)
    return _run_case_real(ctx, cfg, case)


# --- the real agent -------------------------------------------------------


def _run_case_real(ctx: RunContext, cfg: Config, case: dict) -> CaseResult:
    case_id = case["id"]
    prompt = case.get("prompt", "")
    expect = str(case.get("expect_contains", "")).lower()

    started = time.perf_counter()
    spans = 1  # the case span itself
    hops = 0
    tool_calls: list[str] = []
    transcript: list[str] = []
    in_tokens = out_tokens = 0
    cost = 0.0
    answer = ""
    replayed_all = True

    with instrument.case_span(ctx, case_id) as case_sp:
        trace_id = f"{case_sp.get_span_context().trace_id:032x}"
        log.info("case %s starting: %s", case_id, prompt)

        # --- grounding retrieval hop (always exactly one) ------------------
        with instrument.retrieval_span(ctx, name="policy-kb", query=prompt) as rsp:
            grounding = data.search_policies(prompt, limit=2)
            rsp.set_attribute("preflight.retrieval.hits", len(grounding))
        spans += 1
        hops += 1

        context_block = "\n".join(f"- {d['title']}: {d['text']}" for d in grounding)
        messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    f"Relevant policy context:\n{context_block}\n\n"
                    f"Customer question: {prompt}"
                ),
            }
        ]

        for turn in range(MAX_MODEL_TURNS):
            with instrument.llm_span(ctx, cfg, model=MODEL, provider="anthropic") as result:
                response, was_replayed = replay.complete(
                    cfg,
                    model=MODEL,
                    system=SYSTEM_PROMPT,
                    messages=messages,
                    tools=TOOLS,
                    max_tokens=MAX_TOKENS,
                )
                result.response_model = response.get("model") or MODEL
                result.input_tokens = response["usage"]["input_tokens"]
                result.output_tokens = response["usage"]["output_tokens"]
            spans += 1
            replayed_all = replayed_all and was_replayed
            in_tokens += result.input_tokens
            out_tokens += result.output_tokens
            cost += cfg.price(result.response_model).cost_usd(
                result.input_tokens, result.output_tokens
            )

            blocks = response.get("content", [])
            text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            if text:
                answer = text
                transcript.append(text)

            requests = [b for b in blocks if b.get("type") == "tool_use"]
            if not requests:
                break

            messages.append({"role": "assistant", "content": blocks})
            results_block: list[dict] = []
            for req in requests:
                name = req.get("name", "")
                call_id = req.get("id", f"{case_id}-{len(tool_calls)}")
                with instrument.tool_span(ctx, name=name, call_id=call_id) as tsp:
                    payload = _dispatch(ctx, name, req.get("input") or {})
                    tsp.set_attribute("preflight.tool.result_bytes", len(json.dumps(payload)))
                spans += 1
                tool_calls.append(name)
                log.info("case %s tool %s -> %s", case_id, name, payload)
                if name == "policy_search":
                    # policy_search nests a retrieval span; see _dispatch.
                    spans += 1
                    hops += 1
                results_block.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": json.dumps(payload),
                    }
                )
            messages.append({"role": "user", "content": results_block})

            # The last allowed turn already ran the tools above; we stop here
            # rather than spend a third call. The extra tool spans still land,
            # which is exactly what an inflated trajectory looks like.
            if turn == MAX_MODEL_TURNS - 1:
                break

        haystack = " ".join(transcript).lower()
        success = (expect in haystack) if expect else bool(answer)

        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        case_sp.set_attribute("preflight.success", 1 if success else 0)
        case_sp.set_attribute("preflight.tool_calls", len(tool_calls))
        case_sp.set_attribute("preflight.retrieval_hops", hops)
        case_sp.set_attribute("preflight.spans", spans)
        log.info(
            "case %s done: success=%s tools=%s hops=%d tokens=%d",
            case_id,
            success,
            tool_calls,
            hops,
            in_tokens + out_tokens,
        )

    return CaseResult(
        case_id=case_id,
        answer=answer,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        tool_calls=tool_calls,
        success=success,
        cost_usd=cost,
        duration_ms=duration_ms,
        retrieval_hops=hops,
        spans=spans,
        replayed=replayed_all,
        trace_id=trace_id,
        transcript=transcript,
    )


def _dispatch(ctx: RunContext, name: str, args: dict) -> dict:
    """Execute one tool. `policy_search` is the one that costs a retrieval hop."""
    if name == "lookup_order":
        return data.lookup_order(str(args.get("order_id", "")))
    if name == "check_inventory":
        return data.check_inventory(str(args.get("sku", "")))
    if name == "policy_search":
        query = str(args.get("query", ""))
        with instrument.retrieval_span(ctx, name="policy-kb", query=query) as rsp:
            hits = data.search_policies(query, limit=2)
            rsp.set_attribute("preflight.retrieval.hits", len(hits))
        return {"results": [{"title": h["title"], "text": h["text"]} for h in hits]}
    return {"error": f"unknown tool {name!r}"}


# --- the M1 stub, kept behind a flag --------------------------------------


def _deterministic_tokens(seed: str) -> tuple[int, int]:
    digest = hashlib.sha256(seed.encode()).digest()
    return 400 + digest[0] * 2, 80 + digest[1]


def _run_case_stub(ctx: RunContext, cfg: Config, case: dict) -> CaseResult:
    """M1's deterministic stub. No credentials, no cassettes, no spend."""
    case_id = case["id"]
    prompt = case.get("prompt", "")
    started = time.perf_counter()
    spans = 1
    tool_calls: list[str] = []

    with instrument.case_span(ctx, case_id) as case_sp:
        trace_id = f"{case_sp.get_span_context().trace_id:032x}"
        with instrument.retrieval_span(ctx, name="policy-kb", query=prompt):
            time.sleep(0.02)
        spans += 1

        for seed in (f"{case_id}:plan", f"{case_id}:answer"):
            with instrument.llm_span(ctx, cfg, model=M1_MODEL, provider="anthropic") as result:
                time.sleep(0.05)
                result.input_tokens, result.output_tokens = _deterministic_tokens(seed)
            spans += 1

        for i, tool in enumerate(case.get("tools", ["lookup_order"])):
            with instrument.tool_span(ctx, name=tool, call_id=f"{case_id}-{i}"):
                time.sleep(0.01)
            spans += 1
            tool_calls.append(tool)

        in1, out1 = _deterministic_tokens(f"{case_id}:plan")
        in2, out2 = _deterministic_tokens(f"{case_id}:answer")
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        case_sp.set_attribute("preflight.success", 1)
        case_sp.set_attribute("preflight.tool_calls", len(tool_calls))
        case_sp.set_attribute("preflight.retrieval_hops", 1)
        case_sp.set_attribute("preflight.spans", spans)

    price = cfg.price(M1_MODEL)
    return CaseResult(
        case_id=case_id,
        answer=f"stub answer for {case_id}",
        input_tokens=in1 + in2,
        output_tokens=out1 + out2,
        tool_calls=tool_calls,
        success=True,
        cost_usd=price.cost_usd(in1 + in2, out1 + out2),
        duration_ms=duration_ms,
        retrieval_hops=1,
        spans=spans,
        replayed=True,
        trace_id=trace_id,
    )
