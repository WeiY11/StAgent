"""Boundary contracts for the sandbox subprocess runner protocol."""

from __future__ import annotations

import json

import pytest

from leagent.code.runner import _load_runner_payload, build_runner_stdin


def test_v2_runner_payload_round_trip() -> None:
    payload = {"source": "result = '你好'", "timeout_sec": 3.5}

    assert _load_runner_payload(build_runner_stdin(payload)) == {
        **payload,
        "runner_protocol": 2,
    }


@pytest.mark.parametrize("raw", [json.dumps([]).encode(), json.dumps("bad").encode()])
def test_legacy_runner_payload_requires_object(raw: bytes) -> None:
    with pytest.raises(ValueError, match="JSON object"):
        _load_runner_payload(raw)
