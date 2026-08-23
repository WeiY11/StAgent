"""Prometheus cardinality guard for the cache pressure/fill metrics.

Cache pressure metrics must stay low-cardinality: only the logical
``cache_name`` may be a label. Owner, resource, session, model and rejection
detail must never reach Prometheus.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry

from StAgent.utils.metrics import StAgentMetrics


def test_cache_pressure_metrics_use_only_cache_name_label() -> None:
    metrics = StAgentMetrics(CollectorRegistry())
    metrics.record_cache_rejection("chat")
    metrics.record_cache_fill("chat", duration_sec=0.01, coalesced=4)
    payload = metrics.generate_metrics().decode("utf-8")
    assert 'cache_name="chat"' in payload
    assert "owner_id" not in payload
    assert "resource_id" not in payload
