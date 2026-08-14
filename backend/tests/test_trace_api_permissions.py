from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from leagent.api.v1 import traces as traces_api


def _trace_row(trace_id: str, *, user_id: object, experiment_id: str | None = None):
    now = datetime.utcnow()
    return SimpleNamespace(
        trace_id=trace_id,
        parent_trace_id=None,
        session_id=None,
        user_id=str(user_id) if user_id is not None else None,
        scope="chat_turn",
        agent_name="default_agent",
        model="m",
        status="completed",
        terminal_reason="completed",
        started_at=now,
        ended_at=now,
        latency_ms=1.0,
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_miss_tokens=0,
        total_cost_usd=0.0,
        tool_call_count=0,
        llm_call_count=1,
        experiment_id=experiment_id,
        prompt_hash=None,
        tags="{}",
        error=None,
        scores="{}",
        root_span_id="root",
    )


def _span_row(trace_id: str):
    now = datetime.utcnow()
    return SimpleNamespace(
        span_id="span-1",
        parent_span_id=None,
        trace_id=trace_id,
        seq=1,
        kind="agent",
        name="agent",
        status="ok",
        started_at=now,
        ended_at=now,
        latency_ms=1.0,
        attrs="{}",
        input_preview=None,
        output_preview=None,
        payload_ref=None,
    )


def _experiment_row(experiment_id: str, *, created_by: object):
    now = datetime.utcnow()
    return SimpleNamespace(
        experiment_id=experiment_id,
        name="exp",
        prompt="say hi",
        session_id=None,
        model_ids=json.dumps(["m1"]),
        created_by=str(created_by) if created_by is not None else None,
        status="pending",
        error=None,
        created_at=now,
    )


class _FakeTraceStore:
    def __init__(self) -> None:
        self.traces: dict[str, object] = {}
        self.spans: dict[str, list[object]] = {}
        self.experiments: dict[str, object] = {}
        self.list_traces_user_id: str | None = None
        self.stats_user_id: str | None = None
        self.list_experiments_created_by: str | None = None

    async def get_trace(self, trace_id: str):
        return self.traces.get(trace_id)

    async def list_spans(self, trace_id: str):
        return list(self.spans.get(trace_id, []))

    async def list_traces(
        self,
        *,
        user_id: str | None = None,
        experiment_id: str | None = None,
        **_kwargs,
    ):
        self.list_traces_user_id = user_id
        rows = list(self.traces.values())
        if user_id is not None:
            rows = [r for r in rows if getattr(r, "user_id", None) == user_id]
        if experiment_id is not None:
            rows = [r for r in rows if getattr(r, "experiment_id", None) == experiment_id]
        return rows

    async def stats_by_model(self, *, days: int = 30, user_id: str | None = None):
        self.stats_user_id = user_id
        return [
            {
                "model": "m",
                "runs": 1,
                "successes": 1,
                "errors": 0,
                "success_rate": 1.0,
                "avg_latency_ms": 1.0,
                "p50_latency_ms": 1.0,
                "p95_latency_ms": 1.0,
                "avg_input_tokens": 1.0,
                "avg_output_tokens": 1.0,
                "avg_cost_usd": 0.0,
                "avg_tool_calls": 0.0,
                "total_input_tokens": 1,
                "total_output_tokens": 1,
                "total_cost_usd": 0.0,
            }
        ]

    async def list_experiments(
        self,
        *,
        created_by: str | None = None,
        **_kwargs,
    ):
        self.list_experiments_created_by = created_by
        rows = list(self.experiments.values())
        if created_by is not None:
            rows = [r for r in rows if getattr(r, "created_by", None) == created_by]
        return rows

    async def get_experiment(self, experiment_id: str):
        return self.experiments.get(experiment_id)


@pytest.fixture()
def fake_trace_store(monkeypatch: pytest.MonkeyPatch) -> _FakeTraceStore:
    store = _FakeTraceStore()
    monkeypatch.setattr(traces_api, "get_trace_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_trace_list_and_stats_are_user_scoped(
    fake_trace_store: _FakeTraceStore,
) -> None:
    owner = uuid4()
    other = uuid4()
    fake_trace_store.traces["owned"] = _trace_row("owned", user_id=owner)
    fake_trace_store.traces["other"] = _trace_row("other", user_id=other)

    rows = await traces_api.list_traces(owner)
    stats = await traces_api.stats_by_model(owner)

    assert [r.trace_id for r in rows] == ["owned"]
    assert fake_trace_store.list_traces_user_id == str(owner)
    assert len(stats) == 1
    assert fake_trace_store.stats_user_id == str(owner)


@pytest.mark.asyncio
async def test_trace_detail_spans_and_export_reject_non_owner(
    fake_trace_store: _FakeTraceStore,
) -> None:
    owner = uuid4()
    other = uuid4()
    fake_trace_store.traces["other"] = _trace_row("other", user_id=other)
    fake_trace_store.spans["other"] = [_span_row("other")]

    with pytest.raises(HTTPException) as detail_exc:
        await traces_api.get_trace("other", owner)
    with pytest.raises(HTTPException) as spans_exc:
        await traces_api.list_trace_spans("other", owner)
    with pytest.raises(HTTPException) as export_exc:
        await traces_api.export_trace("other", owner)

    assert detail_exc.value.status_code == 404
    assert spans_exc.value.status_code == 404
    assert export_exc.value.status_code == 404


@pytest.mark.asyncio
async def test_experiment_list_and_detail_are_user_scoped(
    fake_trace_store: _FakeTraceStore,
) -> None:
    owner = uuid4()
    other = uuid4()
    fake_trace_store.experiments["owned-exp"] = _experiment_row(
        "owned-exp",
        created_by=owner,
    )
    fake_trace_store.experiments["other-exp"] = _experiment_row(
        "other-exp",
        created_by=other,
    )
    fake_trace_store.traces["owned-trace"] = _trace_row(
        "owned-trace",
        user_id=owner,
        experiment_id="owned-exp",
    )
    fake_trace_store.traces["other-trace"] = _trace_row(
        "other-trace",
        user_id=other,
        experiment_id="owned-exp",
    )

    rows = await traces_api.list_experiments(owner)
    detail = await traces_api.get_experiment("owned-exp", owner)

    assert [r.experiment_id for r in rows] == ["owned-exp"]
    assert fake_trace_store.list_experiments_created_by == str(owner)
    assert [t.trace_id for t in detail.traces] == ["owned-trace"]

    with pytest.raises(HTTPException) as exc:
        await traces_api.get_experiment("other-exp", owner)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_trace_span_returns_full_payload(
    fake_trace_store: _FakeTraceStore,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid4()
    fake_trace_store.traces["t1"] = _trace_row("t1", user_id=owner)

    payload_dir = tmp_path / "traces" / "t1"
    payload_dir.mkdir(parents=True)
    payload_file = payload_dir / "span-1.json"
    payload_file.write_text(
        json.dumps({"input": "full input", "output": {"text": "full output"}}),
        encoding="utf-8",
    )

    row = _span_row("t1")
    row.payload_ref = str(payload_file)
    row.input_preview = "prev-in"
    row.output_preview = "prev-out"
    fake_trace_store.spans["t1"] = [row]

    from leagent.config import constants as trace_constants

    monkeypatch.setattr(trace_constants, "TRACES_DIR", str(tmp_path / "traces"))

    detail = await traces_api.get_trace_span("t1", "span-1", owner)

    assert detail.input_full == "full input"
    assert detail.output_full == json.dumps(
        {"text": "full output"}, ensure_ascii=False
    )
    assert detail.input_preview == "prev-in"


@pytest.mark.asyncio
async def test_get_trace_span_falls_back_to_preview(
    fake_trace_store: _FakeTraceStore,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid4()
    fake_trace_store.traces["t1"] = _trace_row("t1", user_id=owner)

    row = _span_row("t1")
    # No payload file: preview must be used as-is.
    row.input_preview = "prev-in"
    row.output_preview = "prev-out"
    fake_trace_store.spans["t1"] = [row]

    from leagent.config import constants as trace_constants

    monkeypatch.setattr(trace_constants, "TRACES_DIR", str(tmp_path / "traces"))

    detail = await traces_api.get_trace_span("t1", "span-1", owner)

    assert detail.input_full == "prev-in"
    assert detail.output_full == "prev-out"

    # Payload reference escaping TRACES_DIR must be ignored (preview wins).
    escaping = tmp_path / "outside.json"
    escaping.write_text(json.dumps({"input": "evil"}), encoding="utf-8")
    row.payload_ref = str(escaping)
    detail = await traces_api.get_trace_span("t1", "span-1", owner)
    assert detail.input_full == "prev-in"


@pytest.mark.asyncio
async def test_get_trace_span_not_found(
    fake_trace_store: _FakeTraceStore,
) -> None:
    owner = uuid4()
    fake_trace_store.traces["t1"] = _trace_row("t1", user_id=owner)
    fake_trace_store.spans["t1"] = [_span_row("t1")]

    with pytest.raises(HTTPException) as exc:
        await traces_api.get_trace_span("t1", "missing-span", owner)
    assert exc.value.status_code == 404
