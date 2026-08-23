from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from StAgent.db.models.message import Message, MessageRead, MessageRole
from StAgent.services.chat.service import ChatService


def test_message_read_parses_json_text_fields() -> None:
    message = Message(
        id=uuid4(),
        session_id=uuid4(),
        user_id=uuid4(),
        role=MessageRole.ASSISTANT,
        content="Done",
        tool_call_id="call-1",
        tool_calls=json.dumps(
            [
                {
                    "id": "call-1",
                    "name": "excel_generator",
                    "arguments": {"sheet_name": "contacts"},
                    "status": "success",
                },
            ],
        ),
        attachments=json.dumps(
            [
                {
                    "id": "att-1",
                    "filename": "contacts.pdf",
                    "content_type": "application/pdf",
                    "size": 1234,
                },
            ],
        ),
        extensions=json.dumps({"chat_workflow_digest": "abc123"}),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    read = MessageRead.model_validate(message)

    assert read.tool_calls == [
        {
            "id": "call-1",
            "name": "excel_generator",
            "arguments": {"sheet_name": "contacts"},
            "status": "success",
        },
    ]
    assert read.attachments == [
        {
            "id": "att-1",
            "filename": "contacts.pdf",
            "content_type": "application/pdf",
            "size": 1234,
        },
    ]
    assert read.extensions == {"chat_workflow_digest": "abc123"}
    assert read.tool_call_id == "call-1"


@pytest.mark.asyncio
async def test_chat_context_preserves_structured_tool_calls() -> None:
    now = datetime.utcnow()
    message = MessageRead.model_validate(
        Message(
            id=uuid4(),
            session_id=uuid4(),
            role=MessageRole.ASSISTANT,
            content="Done",
            tool_calls=json.dumps([{"id": "call-1", "name": "search"}]),
            tool_call_id="call-1",
            created_at=now,
            updated_at=now,
        ),
    )
    service = ChatService.__new__(ChatService)
    service._settings = SimpleNamespace(agent=SimpleNamespace(context_window_tokens=1000))
    service.get_history = AsyncMock(return_value=[message])

    context = await service.get_context_messages(message.session_id)

    assert context == [
        {
            "role": "assistant",
            "content": "Done",
            "tool_calls": [{"id": "call-1", "name": "search"}],
            "tool_call_id": "call-1",
        },
    ]
