"""Boundary tests for malformed or partial provider routing configuration."""

from __future__ import annotations

from leagent.llm.model_registry import ModelRegistry


def test_registry_treats_non_collection_sections_as_empty() -> None:
    registry = ModelRegistry()

    registry.load_from_config({"providers": None, "routing": None})

    assert registry.list_specs() == []
    assert registry.failover_enabled is False
    assert registry.failover_max_retries == 2


def test_registry_ignores_malformed_nested_routing_sections() -> None:
    registry = ModelRegistry()

    registry.load_from_config(
        {
            "providers": [],
            "routing": {"tasks": None, "fallbacks": [], "failover": "invalid"},
        }
    )

    assert registry.list_specs() == []
    assert registry.failover_enabled is False
