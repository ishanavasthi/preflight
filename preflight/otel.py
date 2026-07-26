"""Tracer / meter setup and the OTLP-over-HTTP exporter.

Hand-rolled rather than auto-instrumented, on purpose: the gate reads spans back
out of SigNoz by attribute name, so the attribute names have to be exactly what
the GenAI semantic conventions say -- not whatever an auto-instrumentation
library happens to emit this month.
"""

from __future__ import annotations

import atexit
import logging

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from preflight.config import Config

INSTRUMENTATION_NAME = "preflight"

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None
_logger_provider: LoggerProvider | None = None


def setup(cfg: Config, *, commit_sha: str) -> None:
    """Install global tracer, meter and logger providers. Idempotent per process."""
    global _tracer_provider, _meter_provider, _logger_provider
    if _tracer_provider is not None:
        return

    resource = Resource.create(
        {
            "service.name": cfg.service_name,
            "service.version": "0.1.0",
            # Resource-level so every span from the run carries the SHA even if
            # a span-level attribute is ever dropped.
            "vcs.commit_sha": commit_sha,
        }
    )

    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{cfg.otlp_endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(_tracer_provider)

    _meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{cfg.otlp_endpoint}/v1/metrics"),
                export_interval_millis=5_000,
            )
        ],
    )
    metrics.set_meter_provider(_meter_provider)

    # Logs go to SigNoz over the same OTLP endpoint. The SDK's LoggingHandler
    # stamps the active span's trace_id/span_id onto every record, so a log
    # line the agent writes inside a case is one click from the span that
    # produced it -- which is the whole "the offending span is one click away"
    # promise, applied to logs as well as traces.
    _logger_provider = LoggerProvider(resource=resource)
    _logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{cfg.otlp_endpoint}/v1/logs"))
    )
    set_logger_provider(_logger_provider)

    agent_log = logging.getLogger("preflight")
    agent_log.setLevel(logging.INFO)
    if not any(isinstance(h, LoggingHandler) for h in agent_log.handlers):
        agent_log.addHandler(
            LoggingHandler(level=logging.INFO, logger_provider=_logger_provider)
        )

    atexit.register(shutdown)


def tracer() -> trace.Tracer:
    return trace.get_tracer(INSTRUMENTATION_NAME)


def meter() -> metrics.Meter:
    return metrics.get_meter(INSTRUMENTATION_NAME)


def force_flush(timeout_millis: int = 30_000) -> None:
    """Block until buffered telemetry has been handed to the collector.

    This is only half the story -- the collector still has to land it in
    ClickHouse. `runner.wait_for_ingest` covers the other half.
    """
    if _tracer_provider is not None:
        _tracer_provider.force_flush(timeout_millis)
    if _meter_provider is not None:
        _meter_provider.force_flush(timeout_millis)
    if _logger_provider is not None:
        _logger_provider.force_flush(timeout_millis)


def shutdown() -> None:
    global _tracer_provider, _meter_provider, _logger_provider
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
        _tracer_provider = None
    if _meter_provider is not None:
        _meter_provider.shutdown()
        _meter_provider = None
    if _logger_provider is not None:
        _logger_provider.shutdown()
        _logger_provider = None
