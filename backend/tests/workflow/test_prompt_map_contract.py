"""Contract tests for prompt-to-execution lookup implementations."""

from __future__ import annotations

from uuid import uuid4

import pytest

from leagent.workflow.prompt_map import build_prompt_map


@pytest.mark.asyncio
async def test_default_prompt_map_round_trip_and_delete() -> None:
    prompt_map = build_prompt_map()
    execution_id = uuid4()

    await prompt_map.set("prompt-1", execution_id)
    assert await prompt_map.get("prompt-1") == execution_id

    await prompt_map.delete("prompt-1")
    assert await prompt_map.get("prompt-1") is None
