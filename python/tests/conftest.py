"""Shared test helpers."""
from __future__ import annotations

from wp_bench.models import ModelGeneration


def fake_generation(text: str) -> ModelGeneration:
    """A ModelGeneration for tests that stub out the provider call."""
    return ModelGeneration(
        text=text,
        raw_response=None,
        retry_count=0,
        latency_ms=1.0,
        provider_response_id="test-response",
    )
