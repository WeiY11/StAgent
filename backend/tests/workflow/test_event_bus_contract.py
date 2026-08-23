"""Contract tests for workflow execution event subscriptions."""

from __future__ import annotations

import asyncio

import pytest

from StAgent.workflow.engine.progress import ProgressEvent
from StAgent.workflow.server.event_bus import InMemoryEventBus


@pytest.mark.asyncio
async def test_prompt_subscription_is_an_async_iterator() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe("prompt-1")
    receive = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)

    event = ProgressEvent(type="execution_start", prompt_id="prompt-1")
    await bus.publish_event("prompt-1", event)

    assert await receive is event
    await subscription.aclose()


@pytest.mark.asyncio
async def test_monitor_subscription_receives_all_prompts() -> None:
    bus = InMemoryEventBus()
    subscription = bus.subscribe_all()
    receive = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)

    event = ProgressEvent(type="execution_start", prompt_id="prompt-2")
    await bus.publish_event("prompt-2", event)

    assert await receive is event
    await subscription.aclose()
