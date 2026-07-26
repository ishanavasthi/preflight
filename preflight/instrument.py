"""Span helpers following the OpenTelemetry GenAI semantic conventions.

Every attribute name emitted here is quoted from the spec rather than invented,
because the gate reads them back by name from SigNoz. Note that the whole
`gen_ai.*` namespace is at **Development** stability -- names may still churn
between spec releases. If a query stops matching, check the spec version first.

Spec: https://opentelemetry.io/docs/specs/semconv/gen-ai/
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from opentelemetry.trace import SpanKind, Status, StatusCode

from preflight import otel
from preflight.config import Config

# --- GenAI semantic convention attribute names (Development stability) ------
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"

# --- Preflight's own attributes: the dimensions the gate groups by ----------
EVAL_RUN_ID = "eval.run_id"
EVAL_CASE_ID = "eval.case_id"
VCS_COMMIT_SHA = "vcs.commit_sha"
PREFLIGHT_COST_USD = "preflight.cost_usd"
PREFLIGHT_SPAN_ROLE = "preflight.span_role"


@dataclass
class RunContext:
    """The dimensions stamped on every span in a single suite run."""

    run_id: str
    commit_sha: str
    case_id: str = ""

    def attributes(self) -> dict[str, str]:
        attrs = {EVAL_RUN_ID: self.run_id, VCS_COMMIT_SHA: self.commit_sha}
        if self.case_id:
            attrs[EVAL_CASE_ID] = self.case_id
        return attrs


@contextmanager
def case_span(ctx: RunContext, case_id: str) -> Iterator[object]:
    """Root span for one golden-suite case. One trace per case."""
    ctx.case_id = case_id
    with otel.tracer().start_as_current_span(
        f"eval.case {case_id}",
        kind=SpanKind.INTERNAL,
        attributes={**ctx.attributes(), PREFLIGHT_SPAN_ROLE: "case"},
    ) as span:
        started = time.perf_counter()
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
        finally:
            span.set_attribute(
                "preflight.duration_ms", round((time.perf_counter() - started) * 1000, 3)
            )


@contextmanager
def llm_span(
    ctx: RunContext,
    cfg: Config,
    *,
    model: str,
    provider: str = "anthropic",
    operation: str = "chat",
) -> Iterator["LLMResult"]:
    """Wrap one LLM call.

    Yields a mutable result the caller fills in with the token counts it got
    back; cost is computed from the price table on exit, so `preflight.cost_usd`
    and the token attributes can never disagree.
    """
    result = LLMResult(response_model=model)
    # Span name convention: "{operation} {request.model}".
    with otel.tracer().start_as_current_span(
        f"{operation} {model}",
        kind=SpanKind.CLIENT,
        attributes={
            **ctx.attributes(),
            GEN_AI_OPERATION_NAME: operation,
            GEN_AI_PROVIDER_NAME: provider,
            GEN_AI_REQUEST_MODEL: model,
            PREFLIGHT_SPAN_ROLE: "llm",
        },
    ) as span:
        try:
            yield result
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
        else:
            span.set_attribute(GEN_AI_RESPONSE_MODEL, result.response_model)
            span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, result.input_tokens)
            span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, result.output_tokens)
            span.set_attribute(
                PREFLIGHT_COST_USD,
                cfg.price(result.response_model).cost_usd(
                    result.input_tokens, result.output_tokens
                ),
            )


@dataclass
class LLMResult:
    response_model: str
    input_tokens: int = 0
    output_tokens: int = 0


@contextmanager
def tool_span(ctx: RunContext, *, name: str, call_id: str) -> Iterator[object]:
    """Wrap one tool invocation."""
    with otel.tracer().start_as_current_span(
        f"execute_tool {name}",
        kind=SpanKind.INTERNAL,
        attributes={
            **ctx.attributes(),
            GEN_AI_OPERATION_NAME: "execute_tool",
            GEN_AI_TOOL_NAME: name,
            GEN_AI_TOOL_CALL_ID: call_id,
            PREFLIGHT_SPAN_ROLE: "tool",
        },
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise


@contextmanager
def retrieval_span(ctx: RunContext, *, name: str, query: str) -> Iterator[object]:
    """Wrap one retrieval hop. Hop count is a gated metric in M3."""
    with otel.tracer().start_as_current_span(
        f"retrieve {name}",
        kind=SpanKind.CLIENT,
        attributes={
            **ctx.attributes(),
            GEN_AI_OPERATION_NAME: "embeddings",
            "preflight.retrieval.source": name,
            "preflight.retrieval.query": query,
            PREFLIGHT_SPAN_ROLE: "retrieval",
        },
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
