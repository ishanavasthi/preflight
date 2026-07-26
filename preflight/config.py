"""Loads preflight.yaml and layers environment overrides on top."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "preflight.yaml"


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok
        ) / 1_000_000


@dataclass(frozen=True)
class Config:
    signoz_url: str
    signoz_api_key: str | None
    otlp_endpoint: str
    service_name: str
    poll_timeout_seconds: int
    poll_interval_seconds: float
    models: dict[str, ModelPrice] = field(default_factory=dict)
    thresholds: dict[str, Any] = field(default_factory=dict)

    def price(self, model: str) -> ModelPrice:
        try:
            return self.models[model]
        except KeyError:
            raise KeyError(
                f"model {model!r} has no entry in preflight.yaml `models:`. "
                "Add its per-MTok prices so the cost math stays auditable."
            ) from None


def load(path: str | Path | None = None) -> Config:
    """Read preflight.yaml. Env vars win over file values."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = yaml.safe_load(cfg_path.read_text()) or {}

    signoz = raw.get("signoz", {})
    otel = raw.get("otel", {})
    ingest = raw.get("ingest", {})

    return Config(
        signoz_url=os.getenv("SIGNOZ_URL", signoz.get("url", "http://localhost:8080")).rstrip("/"),
        signoz_api_key=os.getenv("SIGNOZ_API_KEY"),
        otlp_endpoint=os.getenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT", otel.get("endpoint", "http://localhost:4318")
        ).rstrip("/"),
        service_name=os.getenv("OTEL_SERVICE_NAME", otel.get("service_name", "preflight-agent")),
        poll_timeout_seconds=int(ingest.get("poll_timeout_seconds", 120)),
        poll_interval_seconds=float(ingest.get("poll_interval_seconds", 2)),
        models={
            name: ModelPrice(
                input_per_mtok=float(spec["input_per_mtok"]),
                output_per_mtok=float(spec["output_per_mtok"]),
            )
            for name, spec in (raw.get("models") or {}).items()
        },
        thresholds=raw.get("thresholds") or {},
    )
