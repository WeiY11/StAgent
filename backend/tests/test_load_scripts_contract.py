"""Static contracts for checked-in load-test scripts.

These tests catch stale performance harness drift without requiring k6 or
Locust to be installed in the unit-test environment.
"""

from __future__ import annotations

from pathlib import Path


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_k6_chat_load_script_targets_real_sse_endpoint() -> None:
    text = (_backend_root() / "tests" / "load" / "k6_agent.js").read_text(
        encoding="utf-8"
    )

    assert "/api/v1/chat/stream" in text
    assert "data: [DONE]" in text
    assert "multipart form data" in text


def test_k6_auth_header_is_only_sent_when_token_is_configured() -> None:
    text = (_backend_root() / "tests" / "load" / "k6_agent.js").read_text(
        encoding="utf-8"
    )

    assert "TOKEN ? { Authorization:" in text
    assert "Authorization: `Bearer ${TOKEN}`" in text
    assert "headers: {\n        Accept: 'text/event-stream',\n        Authorization:" not in text
