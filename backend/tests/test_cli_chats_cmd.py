"""Contract tests for local chat CLI fallbacks."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from StAgent.cli import chats_cmd


def test_get_local_chat_rejects_non_object_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir()
    (chats_dir / "chat-1.json").write_text(json.dumps(["not", "a", "chat"]), encoding="utf-8")
    monkeypatch.setattr(chats_cmd, "StAgent_HOME", tmp_path)

    assert chats_cmd._get_local_chat("chat-1") is None
