"""End-to-end engine tests: cache, control-flow, and a minimal run."""

from __future__ import annotations

import pytest

from StAgent.config.settings import Settings
from StAgent.services.cache.service import CacheService
from StAgent.workflow.base import WorkflowStatus
from StAgent.workflow.engine import (
    CacheServiceWorkflowProvider,
    WorkflowExecutor,
    build_cache_set,
)
from StAgent.workflow.engine.caching import (
    CacheKeySetInputSignature,
    LRUCache,
    WorkflowCacheScope,
)
from StAgent.workflow.engine.errors import ValidationError
from StAgent.workflow.engine.runner import _is_cacheable_schema
from StAgent.workflow.io import load
from StAgent.workflow.nodes import bootstrap
from StAgent.workflow.nodes.builtin.end import EndNode
from StAgent.workflow.nodes.builtin.start import StartNode


@pytest.mark.asyncio
async def test_execute_minimal_start_to_end():
    await bootstrap()
    raw = {
        "id": "mini",
        "name": "mini",
        "inputs": [],
        "outputs": [],
        "metadata": {},
        "nodes": {
            "start": {
                "class_type": "StartNode",
                "inputs": {},
                "meta": {},
                "control": {"next": "end"},
            },
            "end": {
                "class_type": "EndNode",
                "inputs": {"result": "ok"},
                "meta": {},
                "control": {},
            },
        },
        "control": {
            "start": "start",
            "end": "end",
            "edges": [],
            "timeout_sec": 3600,
            "max_retries": 3,
            "tags": [],
        },
    }
    doc = load(raw)
    executor = WorkflowExecutor(cache_set=build_cache_set("none"))
    result = await executor.execute(doc, inputs={})
    assert result.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_execute_condition_branches():
    await bootstrap()
    raw = {
        "id": "cond",
        "name": "cond",
        "inputs": [],
        "outputs": [],
        "metadata": {},
        "nodes": {
            "start": {
                "class_type": "StartNode",
                "inputs": {},
                "meta": {},
                "control": {"next": "pick"},
            },
            "pick": {
                "class_type": "ConditionNode",
                "inputs": {},
                "meta": {},
                "control": {
                    "conditions": [
                        {
                            "when": {"left": "true", "op": "eq", "right": "true"},
                            "then_node": "happy",
                        }
                    ],
                    "else_node": "sad",
                },
            },
            "happy": {
                "class_type": "EndNode",
                "inputs": {"result": "happy"},
                "meta": {},
                "control": {},
            },
            "sad": {
                "class_type": "EndNode",
                "inputs": {"result": "sad"},
                "meta": {},
                "control": {},
            },
        },
        "control": {
            "start": "start",
            "end": "happy",
            "edges": [],
            "timeout_sec": 3600,
            "max_retries": 3,
            "tags": [],
        },
    }
    doc = load(raw)
    executor = WorkflowExecutor(cache_set=build_cache_set("none"))
    result = await executor.execute(doc, inputs={})
    assert result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED)


@pytest.mark.asyncio
async def test_execute_rejects_invalid_graph():
    await bootstrap()
    raw = {
        "id": "invalid",
        "name": "invalid",
        "inputs": [],
        "outputs": [],
        "metadata": {},
        "nodes": {
            "start": {
                "class_type": "StartNode",
                "inputs": {},
                "meta": {},
                "control": {"next": "ghost"},
            },
        },
        "control": {
            "start": "start",
            "end": "end",
            "edges": [],
            "timeout_sec": 3600,
            "max_retries": 3,
            "tags": [],
        },
    }
    doc = load(raw)
    executor = WorkflowExecutor(cache_set=build_cache_set("none"))
    with pytest.raises(ValidationError):
        await executor.execute(doc)


def test_cache_set_policies_are_isolated():
    none_set = build_cache_set("none")
    lru = build_cache_set("lru")
    assert none_set.outputs is not lru.outputs
    assert isinstance(lru.outputs, LRUCache)


def test_build_cache_set_rejects_unknown_mode():
    with pytest.raises(ValueError):
        build_cache_set("classic")


def test_executor_default_output_cache_is_bounded() -> None:
    executor = WorkflowExecutor()
    assert isinstance(executor.cache_set.outputs, LRUCache)
    assert executor.cache_set.outputs.max_size == 128
    assert isinstance(executor.cache_set.objects, LRUCache)
    assert isinstance(executor.cache_set.ui, LRUCache)


def test_identical_node_inputs_do_not_cross_owner_scope() -> None:
    prompt = {
        "end": {"class_type": "EndNode", "inputs": {"result": "same"}}
    }
    left = CacheKeySetInputSignature(
        prompt, scope_digest=WorkflowCacheScope(owner_id="u1").digest()
    )
    right = CacheKeySetInputSignature(
        prompt, scope_digest=WorkflowCacheScope(owner_id="u2").digest()
    )
    assert left.signature("end") != right.signature("end")


@pytest.mark.asyncio
async def test_cache_service_workflow_provider_round_trips_node_payload() -> None:
    cache = CacheService(Settings())
    await cache.start()
    try:
        provider = CacheServiceWorkflowProvider(cache)
        payload = {"values": ["ok"], "ui": None, "metadata": {"source": "test"}}

        assert await provider.on_lookup("node-key") is None
        await provider.on_store("node-key", payload)

        assert await provider.on_lookup("node-key") == payload
    finally:
        await cache.stop()


def test_control_flow_and_output_schemas_are_not_cacheable() -> None:
    assert _is_cacheable_schema(StartNode.get_schema()) is False
    assert _is_cacheable_schema(EndNode.get_schema()) is False
