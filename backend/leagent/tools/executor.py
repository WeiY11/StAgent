"""Unified tool execution module.

This is the single, authoritative executor for every caller — chat agents,
subagents, workflow nodes, the MCP bridge, cron jobs, and background
workers. It owns:

* Registry lookup and alias resolution via :class:`ToolRegistry`
* Permission enforcement (:func:`check_tool_permission`) with the real
  :class:`ToolContext` threaded through
* Context construction via :func:`build_tool_context` when the caller
  passes something that isn't already a :class:`ToolContext` (e.g. an
  :class:`AgentContext`)
* Single-tool dispatch (:meth:`execute`, :meth:`run_tool`)
* Batch dispatch with concurrency-safe partitioning, parallel/sequential
  modes, fail-fast, and explicit dependencies
* Optional progress callbacks and cancellation through
  :class:`ToolContext.abort_signal`
* Lightweight execution metrics per tool

There is no separate "agent adapter" class; agents and workflows both
use :class:`ToolExecutor` directly. :class:`ToolResult` is the single
result envelope (see :mod:`leagent.tools.base`).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import structlog

from leagent.tools.base import (
    ToolContext,
    ToolPermissionContext,
    ToolProgressCallback,
    ToolResult,
    check_tool_permission,
)
from leagent.tools.context import build_tool_context
from leagent.tools.pipeline import MiddlewareContext, MiddlewarePipeline
from leagent.tools.rate_limit import tool_rate_limit_from_env
from leagent.tools.registry import ToolNotFoundError, ToolRegistry, get_registry

# JSON tool-argument parsing/repair/recovery lives in a dedicated module;
# re-exported here so existing ``from leagent.tools.executor import ...`` and
# tests that reach for these helpers keep working unchanged.
from leagent.tools.tool_args_parsing import (
    _candidate_json_texts,
    _close_truncated_json_array,
    _close_truncated_json_object,
    _decode_partial_json_string_body,
    _escape_control_chars_in_json_strings,
    _escape_unescaped_quotes_in_json_strings,
    _extract_and_close_truncated_tree,
    _extract_json_object_value,
    _extract_json_string_value,
    _extract_malformed_quoted_field,
    _extract_malformed_source,
    _extract_truncated_string_value,
    _extract_unterminated_json_string_field,
    _find_json_string_end,
    _format_code_execution_raw_args_error,
    _format_tool_argument_blob_raw_args_error,
    _is_json_string_terminator_after_quote,
    _loads_json_dict,
    _looks_like_stream_truncation,
    _recover_canvas_publish_args,
    _recover_code_execution_args,
    _recover_emit_ui_patch_args,
    _recover_emit_ui_tree_args,
    _recover_project_apply_patch_args,
    _recover_project_edit_args,
    _recover_project_write_args,
    _recover_tool_argument_blob_args,
    _repair_trailing_commas,
    _salvage_truncated_json_after_key,
    _salvage_truncated_json_prefix,
    _strip_json_code_fence,
    _strip_json_prefix,
    _tool_json_parse_recovery_hint,
    _try_json_dict_raw_decode_trailing_junk,
    _try_parse_closed_json_prefix,
    _try_parse_raw_tool_args,
    _try_repair_superfluous_closing_delimiter,
    format_tool_arguments_json_error,
    normalize_tool_parameters,
    parse_tool_arguments_str,
    strict_json_loads_error,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from leagent.services.service_manager import ServiceManager
    from leagent.tools.approval import PendingApproval

logger = structlog.get_logger(__name__)


SENSITIVE_KEYS: frozenset[str] = frozenset({
    "password", "token", "secret", "key", "credential", "auth",
})
_AUDIT_VALUE_LIMIT = 20_000


def _json_dumps_limited(value: Any, limit: int = _AUDIT_VALUE_LIMIT) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "\n... [truncated]"


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Dispatch records
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """A single tool invocation request used by batch dispatch."""

    tool_name: str
    parameters: dict[str, Any]
    call_id: str | None = None

    def __post_init__(self) -> None:
        if self.call_id is None:
            self.call_id = f"{self.tool_name}_{id(self)}"


@dataclass
class ExecutionResult:
    """Per-call record returned by the executor."""

    call_id: str
    tool_name: str
    result: ToolResult
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at) * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "result": self.result.to_dict(),
            "duration_ms": self.duration_ms,
        }


@dataclass
class AggregatedResult:
    """Aggregate of multiple :class:`ExecutionResult` instances."""

    results: list[ExecutionResult] = field(default_factory=list)
    total_duration_ms: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def all_successful(self) -> bool:
        return all(r.result.success for r in self.results)

    @property
    def any_successful(self) -> bool:
        return any(r.result.success for r in self.results)

    @property
    def successful_count(self) -> int:
        return sum(1 for r in self.results if r.result.success)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.result.success)

    def get_result(self, call_id: str) -> ExecutionResult | None:
        for result in self.results:
            if result.call_id == call_id:
                return result
        return None

    def get_by_tool(self, tool_name: str) -> list[ExecutionResult]:
        return [r for r in self.results if r.tool_name == tool_name]

    def get_successful(self) -> list[ExecutionResult]:
        return [r for r in self.results if r.result.success]

    def get_failed(self) -> list[ExecutionResult]:
        return [r for r in self.results if not r.result.success]

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [r.to_dict() for r in self.results],
            "total_duration_ms": self.total_duration_ms,
            "successful_count": self.successful_count,
            "failed_count": self.failed_count,
            "all_successful": self.all_successful,
        }


class ToolExecutionError(Exception):
    """Base exception for tool execution errors."""

    def __init__(self, message: str, call_id: str | None = None) -> None:
        self.call_id = call_id
        super().__init__(message)


# ---------------------------------------------------------------------------
# Context coercion
# ---------------------------------------------------------------------------


def _coerce_context(
    value: Any,
    *,
    service_manager: ServiceManager | None = None,
    temp_dir: str | None = None,
    extra: dict[str, Any] | None = None,
) -> ToolContext:
    """Return a :class:`ToolContext` built from ``value``.

    Accepts:
    * ``None`` — build a minimal anonymous context
    * :class:`ToolContext` — returned as-is (with optional extra merge)
    * :class:`leagent.agent.base.AgentContext` or any object exposing
      ``user_id`` / ``session_id`` / ``task_id`` / ``_abort_event``
    """
    if isinstance(value, ToolContext):
        if extra:
            value.extra.update(extra)
        return value

    user_id = getattr(value, "user_id", None) if value is not None else None
    session_id = getattr(value, "session_id", None) if value is not None else None
    task_id = getattr(value, "task_id", None) if value is not None else None
    abort_signal = (
        getattr(value, "abort_event", None)
        or getattr(value, "_abort_event", None)
        if value is not None
        else None
    )

    # Seed from the source object's own .extra dict (e.g. ToolUseContext
    # carries ``extra["attachments"]`` that the sandbox needs).
    source_extra = getattr(value, "extra", None)
    merged_extra: dict[str, Any] = dict(source_extra) if isinstance(source_extra, dict) else {}
    if extra:
        merged_extra.update(extra)
    if value is not None and not isinstance(value, ToolContext):
        merged_extra.setdefault("agent_context", value)

    return build_tool_context(
        service_manager=service_manager,
        user_id=user_id,
        session_id=session_id,
        task_id=task_id,
        abort_signal=abort_signal,
        temp_dir=temp_dir,
        extra=merged_extra,
    )


# ---------------------------------------------------------------------------
# ToolExecutor
# ---------------------------------------------------------------------------


class ToolExecutor:
    """Professional tool executor shared by agents and workflows.

    Construction takes a :class:`ToolRegistry` (defaults to the global
    singleton) and an optional :class:`ServiceManager` used to build rich
    :class:`ToolContext` instances whenever the caller doesn't pass one
    explicitly. Instances are stateful (metrics, semaphore) but safe to
    share across concurrent callers.
    """

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        *,
        default_timeout: float = 300.0,
        max_parallel: int = 10,
        permission_context: ToolPermissionContext | None = None,
        service_manager: ServiceManager | None = None,
    ) -> None:
        self._registry = registry or get_registry()
        self._default_timeout = default_timeout
        self._max_parallel = max_parallel
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._permission_context = permission_context
        self._service_manager = service_manager
        self._metrics: dict[str, list[int]] = {}
        self._pipeline: MiddlewarePipeline | None = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def default_timeout(self) -> float:
        return self._default_timeout

    @property
    def service_manager(self) -> ServiceManager | None:
        return self._service_manager

    def set_service_manager(self, service_manager: ServiceManager | None) -> None:
        """Attach (or clear) the :class:`ServiceManager` used for contexts."""
        self._service_manager = service_manager

    def set_permission_context(self, ctx: ToolPermissionContext | None) -> None:
        self._permission_context = ctx

    def set_pipeline(self, pipeline: MiddlewarePipeline | None) -> None:
        """Attach a custom middleware pipeline."""
        self._pipeline = pipeline

    def approval_requirement(
        self,
        tool_name: str,
        parameters: dict[str, Any],
        context: Any = None,
    ) -> PendingApproval | None:
        """Pre-flight check: does this call need a user approval pause?

        Used by the query loop *before* dispatch so a gated call can
        surface an Allow/Deny card (``AWAITING_USER_INPUT``) instead of
        failing closed. Returns ``None`` when the call may proceed (or
        when it will be hard-denied — hard denials still fail in
        :meth:`execute`).
        """
        from leagent.tools.approval import PendingApproval, params_digest

        if self._permission_context is None:
            return None
        try:
            tool = self._registry.get(tool_name)
        except ToolNotFoundError:
            return None
        tool_ctx = _coerce_context(
            context, service_manager=self._service_manager, temp_dir=None, extra=None,
        )
        perm = check_tool_permission(
            tool, parameters, self._permission_context, tool_context=tool_ctx,
        )
        if not perm.needs_approval:
            return None
        detail = ""
        try:
            detail = json.dumps(self._sanitize_params(parameters), ensure_ascii=False)[:400]
        except Exception:  # noqa: BLE001
            pass
        return PendingApproval(
            tool_call_id="",
            tool_name=tool_name,
            params_digest=params_digest(parameters),
            reason=perm.reason or "approval required",
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Core execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        tool_name: str,
        parameters: dict[str, Any],
        context: Any = None,
        *,
        call_id: str | None = None,
        on_progress: ToolProgressCallback | None = None,
        temp_dir: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """Execute a single tool and return a rich :class:`ExecutionResult`.

        ``context`` may be a :class:`ToolContext`, an :class:`AgentContext`
        (or anything with ``user_id``/``session_id``), or ``None``; it is
        coerced into a full :class:`ToolContext` using the attached
        :class:`ServiceManager`.
        """
        call_id = call_id or f"{tool_name}_{uuid4().hex[:8]}"
        started_at = time.monotonic()

        try:
            tool = self._registry.get(tool_name)
        except ToolNotFoundError:
            logger.error("tool_not_found", tool=tool_name, call_id=call_id)
            return ExecutionResult(
                call_id=call_id,
                tool_name=tool_name,
                result=ToolResult.fail(f"Tool not found: {tool_name}"),
                started_at=started_at,
                finished_at=time.monotonic(),
            )

        if not tool.is_enabled:
            return ExecutionResult(
                call_id=call_id,
                tool_name=tool_name,
                result=ToolResult.fail(f"Tool '{tool_name}' is disabled"),
                started_at=started_at,
                finished_at=time.monotonic(),
            )

        normalized_params, normalize_error = normalize_tool_parameters(parameters, tool=tool)
        if normalize_error:
            raw_for_log = str(parameters.get("__raw__", "") or "")
            strict_err = strict_json_loads_error(raw_for_log) if raw_for_log else None
            result_error = format_tool_arguments_json_error(
                raw_for_log, strict_err, tool_name=tool_name,
            )
            blob_hint = any(
                s in raw_for_log
                for s in ("_blob_id", '"blob_id"')
            )
            logger.warning(
                "tool_args_parse_failed",
                tool=tool_name,
                call_id=call_id,
                args_len=len(raw_for_log),
                json_error=str(strict_err) if strict_err else None,
                json_lineno=getattr(strict_err, "lineno", None),
                json_colno=getattr(strict_err, "colno", None),
                json_pos=getattr(strict_err, "pos", None),
                raw_preview=raw_for_log[:400],
                tool_arguments_json_unrecoverable=True,
                blob_id_hint_present=blob_hint,
            )
            return ExecutionResult(
                call_id=call_id,
                tool_name=tool_name,
                result=ToolResult.fail(result_error),
                started_at=started_at,
                finished_at=time.monotonic(),
            )
        parameters = normalized_params

        merged_ctx_extra: dict[str, Any] = dict(extra or {})
        merged_ctx_extra["current_tool_call_id"] = call_id

        tool_ctx = _coerce_context(
            context,
            service_manager=self._service_manager,
            temp_dir=temp_dir,
            extra=merged_ctx_extra,
        )

        if on_progress:
            on_progress({"type": "tool_start", "tool": tool_name, "call_id": call_id})

        logger.info(
            "tool_execute_start",
            tool=tool_name,
            call_id=call_id,
            params=self._sanitize_params(parameters),
        )

        if self._pipeline is not None:
            mw_ctx = MiddlewareContext(
                tool_name=tool_name,
                parameters=parameters,
                call_id=call_id,
                tool_context=tool_ctx,
                registry=self._registry,
            )
            result = await self._pipeline.execute(
                mw_ctx,
                lambda ctx: self._execute_core(ctx, on_progress=on_progress),
            )
        else:
            if self._permission_context is not None:
                perm = check_tool_permission(
                    tool, parameters, self._permission_context, tool_context=tool_ctx,
                )
                if not perm.allowed:
                    fail = ToolResult.fail(
                        f"Permission denied: {perm.reason}",
                        approval_required=perm.needs_approval,
                    )
                    return ExecutionResult(
                        call_id=call_id,
                        tool_name=tool_name,
                        result=fail,
                        started_at=started_at,
                        finished_at=time.monotonic(),
                    )
                if perm.updated_params:
                    parameters = perm.updated_params

            rate_lim, _ = tool_rate_limit_from_env()
            if rate_lim is not None:
                uid = (getattr(tool_ctx, "user_id", None) or "anon")[:200]
                if not rate_lim.allow(f"{uid}\x00{tool_name}"):
                    return ExecutionResult(
                        call_id=call_id,
                        tool_name=tool_name,
                        result=ToolResult.fail(
                            "Tool rate limit exceeded for this user; retry shortly."
                        ),
                        started_at=started_at,
                        finished_at=time.monotonic(),
                    )

            async with self._semaphore:
                from leagent.telemetry.otel import get_tracer

                tracer = get_tracer("leagent.tools.executor")
                with tracer.start_as_current_span("agent.tool") as _span:
                    if hasattr(_span, "set_attribute"):
                        _span.set_attribute("tool.name", tool_name)
                    result = await tool.run(parameters, tool_ctx, on_progress=on_progress)

        finished_at = time.monotonic()
        execution_result = ExecutionResult(
            call_id=call_id,
            tool_name=tool_name,
            result=result,
            started_at=started_at,
            finished_at=finished_at,
        )

        self._record_metric(tool_name, execution_result.duration_ms, result.success)
        try:
            from leagent.utils.metrics import get_metrics

            get_metrics().record_tool_execution(
                tool_name,
                execution_result.duration_ms / 1000.0,
                result.success,
                "tool_error" if not result.success else None,
            )
        except Exception:  # noqa: BLE001
            logger.debug("tool_prometheus_metrics_failed", tool=tool_name)
        await self._audit_tool_execution(
            tool_name=tool_name,
            parameters=parameters,
            tool_ctx=tool_ctx,
            result=result,
            duration_ms=execution_result.duration_ms,
        )

        if on_progress:
            on_progress({
                "type": "tool_end",
                "tool": tool_name,
                "call_id": call_id,
                "success": result.success,
                "duration_ms": execution_result.duration_ms,
            })

        log_method = logger.info if result.success else logger.warning
        log_method(
            "tool_execute_completed",
            tool=tool_name,
            call_id=call_id,
            success=result.success,
            duration_ms=execution_result.duration_ms,
        )

        return execution_result

    async def _audit_tool_execution(
        self,
        *,
        tool_name: str,
        parameters: dict[str, Any],
        tool_ctx: ToolContext,
        result: ToolResult,
        duration_ms: int,
    ) -> None:
        pass

    async def _execute_core(
        self,
        ctx: MiddlewareContext,
        *,
        on_progress: ToolProgressCallback | None = None,
    ) -> ToolResult:
        """Execute the actual tool run inside the semaphore."""
        tool = self._registry.get(ctx.tool_name)
        async with self._semaphore:
            return await tool.run(ctx.parameters, ctx.tool_context, on_progress=on_progress)

    async def execute_call(
        self,
        call: ToolCall,
        context: Any = None,
        *,
        on_progress: ToolProgressCallback | None = None,
    ) -> ExecutionResult:
        return await self.execute(
            call.tool_name,
            call.parameters,
            context,
            call_id=call.call_id,
            on_progress=on_progress,
        )

    # ------------------------------------------------------------------
    # Convenience facade for the agent layer
    # ------------------------------------------------------------------

    async def run_tool(
        self,
        tool_name: str,
        parameters: dict[str, Any],
        context: Any = None,
        *,
        timeout: int | None = None,
        retries: int | None = None,
        on_progress: ToolProgressCallback | None = None,
        temp_dir: str | None = None,
        call_id: str | None = None,
    ) -> ToolResult:
        """Execute one tool, returning the flat :class:`ToolResult` envelope.

        Convenience used by :class:`AgentController`. Per-call ``timeout``
        and ``retries`` override the tool's class-level defaults for the
        duration of this call.
        """
        tool = self._registry.get_optional(tool_name)
        original_timeout = getattr(tool, "timeout_sec", None) if tool else None
        original_retries = getattr(tool, "max_retries", None) if tool else None
        restore_timeout = timeout is not None and tool is not None
        restore_retries = retries is not None and tool is not None

        try:
            if restore_timeout:
                tool.timeout_sec = int(timeout)  # type: ignore[attr-defined,union-attr]
            if restore_retries:
                tool.max_retries = int(retries)  # type: ignore[attr-defined,union-attr]

            exec_result = await self.execute(
                tool_name,
                parameters,
                context,
                on_progress=on_progress,
                temp_dir=temp_dir,
                call_id=call_id,
            )
        finally:
            if restore_timeout and tool is not None and original_timeout is not None:
                tool.timeout_sec = original_timeout  # type: ignore[attr-defined]
            if restore_retries and tool is not None and original_retries is not None:
                tool.max_retries = original_retries  # type: ignore[attr-defined]

        # Annotate duration from the wrapper when the inner envelope
        # didn't record it (ToolResult.fail paths).
        if exec_result.result.duration_ms == 0:
            exec_result.result.duration_ms = exec_result.duration_ms
        return exec_result.result

    async def run_tools_parallel(
        self,
        tool_calls: Sequence[Any],
        context: Any = None,
        *,
        fail_fast: bool = False,
        timeout: float | None = None,
        on_progress: ToolProgressCallback | None = None,
    ) -> list[ToolResult]:
        """Run multiple tool calls in parallel, returning flat :class:`ToolResult`s.

        Accepts either :class:`ToolCall` or any object with ``.name`` /
        ``.arguments`` (e.g. :class:`leagent.agent.base.ToolCall`).
        Order is preserved.
        """
        calls = [_normalize_call(c) for c in tool_calls]
        if not calls:
            return []
        aggregated = await self.execute_parallel(
            calls, context,
            timeout=timeout, fail_fast=fail_fast, on_progress=on_progress,
        )
        by_id = {r.call_id: r.result for r in aggregated.results}
        out: list[ToolResult] = []
        for c in calls:
            result = by_id.get(c.call_id or "")
            out.append(result or ToolResult.fail("Missing execution result"))
        return out

    async def run_tools_sequential(
        self,
        tool_calls: Sequence[Any],
        context: Any = None,
        *,
        stop_on_error: bool = True,
        on_progress: ToolProgressCallback | None = None,
    ) -> list[ToolResult]:
        calls = [_normalize_call(c) for c in tool_calls]
        results: list[ToolResult] = []
        for call in calls:
            exec_result = await self.execute_call(call, context, on_progress=on_progress)
            results.append(exec_result.result)
            if stop_on_error and not exec_result.result.success:
                break
        return results

    # ------------------------------------------------------------------
    # Batch dispatch primitives
    # ------------------------------------------------------------------

    def partition_calls(
        self, calls: Sequence[ToolCall],
    ) -> tuple[list[ToolCall], list[ToolCall]]:
        """Partition calls into (concurrent_safe, serial) groups."""
        concurrent_safe: list[ToolCall] = []
        serial: list[ToolCall] = []
        for call in calls:
            tool = self._registry.get_optional(call.tool_name)
            if tool and getattr(tool, "is_concurrency_safe", False):
                concurrent_safe.append(call)
            else:
                serial.append(call)
        return concurrent_safe, serial

    async def execute_partitioned(
        self,
        calls: Sequence[ToolCall],
        context: Any = None,
        *,
        on_progress: ToolProgressCallback | None = None,
    ) -> AggregatedResult:
        """Concurrent-safe tools run in parallel; the rest serially."""
        if not calls:
            return AggregatedResult()

        started_at = time.monotonic()
        concurrent_safe, serial = self.partition_calls(calls)
        all_results: list[ExecutionResult] = []

        if concurrent_safe:
            parallel = await self.execute_parallel(
                concurrent_safe, context, on_progress=on_progress,
            )
            all_results.extend(parallel.results)

        for call in serial:
            result = await self.execute_call(call, context, on_progress=on_progress)
            all_results.append(result)

        finished_at = time.monotonic()
        try:
            from leagent.utils.metrics import get_metrics

            get_metrics().record_agent_turn_phase(
                "tool_execute_partitioned",
                finished_at - started_at,
                status="success" if all(r.result.success for r in all_results) else "failure",
            )
        except Exception:
            logger.debug("tool_partition_metrics_failed", exc_info=True)
        return AggregatedResult(
            results=all_results,
            total_duration_ms=int((finished_at - started_at) * 1000),
            started_at=started_at,
            finished_at=finished_at,
        )

    async def execute_parallel(
        self,
        calls: Sequence[ToolCall],
        context: Any = None,
        *,
        timeout: float | None = None,
        fail_fast: bool = False,
        on_progress: ToolProgressCallback | None = None,
    ) -> AggregatedResult:
        """Execute many calls concurrently with optional fail-fast."""
        if not calls:
            return AggregatedResult()

        timeout = timeout or self._default_timeout
        started_at = time.monotonic()

        logger.info(
            "tool_execute_parallel",
            tool_count=len(calls),
            tools=[c.tool_name for c in calls],
            timeout=timeout,
        )

        tasks: list[asyncio.Task[ExecutionResult]] = [
            asyncio.create_task(
                self.execute_call(call, context, on_progress=on_progress),
                name=f"tool_{call.call_id}",
            )
            for call in calls
        ]

        if fail_fast:
            results = await self._execute_fail_fast(tasks, timeout)
        else:
            results = await self._execute_wait_all(tasks, timeout)

        finished_at = time.monotonic()
        aggregated = AggregatedResult(
            results=results,
            total_duration_ms=int((finished_at - started_at) * 1000),
            started_at=started_at,
            finished_at=finished_at,
        )
        logger.info(
            "tool_execute_parallel_done",
            total_duration_ms=aggregated.total_duration_ms,
            successful=aggregated.successful_count,
            failed=aggregated.failed_count,
        )
        try:
            from leagent.utils.metrics import get_metrics

            get_metrics().record_agent_turn_phase(
                "tool_execute_parallel",
                finished_at - started_at,
                status="success" if aggregated.failed_count == 0 else "failure",
            )
        except Exception:
            logger.debug("tool_parallel_metrics_failed", exc_info=True)
        return aggregated

    async def _execute_wait_all(
        self,
        tasks: list[asyncio.Task[ExecutionResult]],
        timeout: float,
    ) -> list[ExecutionResult]:
        results: list[ExecutionResult] = []
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.ALL_COMPLETED,
            )
            for task in done:
                try:
                    results.append(task.result())
                except Exception as e:  # noqa: BLE001
                    logger.error("tool_task_failed", error=str(e))
            for task in pending:
                task.cancel()
                task_name = task.get_name()
                results.append(
                    ExecutionResult(
                        call_id=task_name,
                        tool_name=task_name.replace("tool_", "", 1),
                        result=ToolResult.fail(f"Execution timed out after {timeout}s"),
                        started_at=0,
                        finished_at=time.monotonic(),
                    )
                )
        except Exception as e:  # noqa: BLE001
            logger.error("tool_execute_parallel_failed", error=str(e))
            for task in tasks:
                if not task.done():
                    task.cancel()
        return results

    async def _execute_fail_fast(
        self,
        tasks: list[asyncio.Task[ExecutionResult]],
        timeout: float,
    ) -> list[ExecutionResult]:
        results: list[ExecutionResult] = []
        pending = set(tasks)
        try:
            async with asyncio.timeout(timeout):
                while pending:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        try:
                            result = task.result()
                            results.append(result)
                            if not result.result.success:
                                logger.warning(
                                    "tool_fail_fast_cancel",
                                    failed_tool=result.tool_name,
                                    remaining=len(pending),
                                )
                                for p in pending:
                                    p.cancel()
                                pending.clear()
                                break
                        except Exception as e:  # noqa: BLE001
                            logger.error("tool_task_failed", error=str(e))
        except TimeoutError:
            logger.warning("tool_fail_fast_timeout", timeout=timeout)
            for task in pending:
                task.cancel()
        return results

    async def execute_sequential(
        self,
        calls: Sequence[ToolCall],
        context: Any = None,
        *,
        stop_on_failure: bool = False,
        on_progress: ToolProgressCallback | None = None,
    ) -> AggregatedResult:
        if not calls:
            return AggregatedResult()

        started_at = time.monotonic()
        results: list[ExecutionResult] = []
        for call in calls:
            result = await self.execute_call(call, context, on_progress=on_progress)
            results.append(result)
            if stop_on_failure and not result.result.success:
                logger.warning(
                    "tool_sequential_stopped",
                    failed_tool=call.tool_name,
                    completed=len(results),
                    remaining=len(calls) - len(results),
                )
                break

        finished_at = time.monotonic()
        return AggregatedResult(
            results=results,
            total_duration_ms=int((finished_at - started_at) * 1000),
            started_at=started_at,
            finished_at=finished_at,
        )

    async def execute_with_dependencies(
        self,
        calls: dict[str, ToolCall],
        dependencies: dict[str, list[str]],
        context: Any = None,
    ) -> AggregatedResult:
        started_at = time.monotonic()
        results: dict[str, ExecutionResult] = {}
        completed: set[str] = set()

        def get_ready() -> list[str]:
            ready = []
            for call_id in calls:
                if call_id in completed:
                    continue
                deps = dependencies.get(call_id, [])
                if all(d in completed for d in deps):
                    ready.append(call_id)
            return ready

        while len(completed) < len(calls):
            ready = get_ready()
            if not ready:
                missing = set(calls.keys()) - completed
                logger.error("tool_dependency_cycle", unresolved=list(missing))
                break
            batch_calls = [calls[call_id] for call_id in ready]
            batch_result = await self.execute_parallel(batch_calls, context)
            for exec_result in batch_result.results:
                results[exec_result.call_id] = exec_result
                completed.add(exec_result.call_id)

        finished_at = time.monotonic()
        return AggregatedResult(
            results=list(results.values()),
            total_duration_ms=int((finished_at - started_at) * 1000),
            started_at=started_at,
            finished_at=finished_at,
        )

    # ------------------------------------------------------------------
    # Metrics and introspection
    # ------------------------------------------------------------------

    def get_tool_stats(self, tool_name: str) -> dict[str, Any]:
        durations = self._metrics.get(tool_name, [])
        if not durations:
            return {
                "count": 0,
                "avg_duration_ms": 0,
                "min_duration_ms": 0,
                "max_duration_ms": 0,
            }
        return {
            "count": len(durations),
            "avg_duration_ms": sum(durations) // len(durations),
            "min_duration_ms": min(durations),
            "max_duration_ms": max(durations),
        }

    def _record_metric(self, tool_name: str, duration_ms: int, success: bool) -> None:
        bucket = self._metrics.setdefault(tool_name, [])
        bucket.append(duration_ms)
        if len(bucket) > 1000:
            del bucket[:500]

    def _sanitize_params(self, params: dict[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for k, v in (params or {}).items():
            key_lower = k.lower()
            if any(s in key_lower for s in SENSITIVE_KEYS):
                sanitized[k] = "***REDACTED***"
            elif isinstance(v, str) and len(v) > 500:
                sanitized[k] = v[:100] + "...[truncated]"
            else:
                sanitized[k] = v
        return sanitized

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Shutdown hook. The executor owns no long-lived resources."""
        return

    @classmethod
    def get_default(cls) -> ToolExecutor:
        """Return the process-wide default executor."""
        return get_executor()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_call(call: Any) -> ToolCall:
    """Coerce any call-like object into a :class:`ToolCall`."""
    if isinstance(call, ToolCall):
        return call
    # leagent.agent.base.ToolCall (pydantic) exposes .name and .arguments
    name = getattr(call, "name", None) or getattr(call, "tool_name", None)
    args = getattr(call, "arguments", None) or getattr(call, "parameters", None) or {}
    call_id = getattr(call, "id", None) or getattr(call, "call_id", None)
    if not name:
        raise ValueError(f"Cannot derive tool name from {call!r}")
    return ToolCall(tool_name=name, parameters=dict(args), call_id=call_id)


_default_executor: ToolExecutor | None = None


def get_executor() -> ToolExecutor:
    """Return the process-wide default :class:`ToolExecutor`."""
    global _default_executor
    if _default_executor is None:
        _default_executor = ToolExecutor()
    return _default_executor


def reset_executor() -> None:
    """Reset the default executor (mainly for testing)."""
    global _default_executor
    _default_executor = None


__all__ = [
    "ToolCall",
    "ExecutionResult",
    "AggregatedResult",
    "ToolExecutionError",
    "ToolExecutor",
    "get_executor",
    "reset_executor",
    # Re-exported from ``leagent.tools.tool_args_parsing`` for backward
    # compatibility (production code and tests import these from here).
    "_candidate_json_texts",
    "_close_truncated_json_array",
    "_close_truncated_json_object",
    "_decode_partial_json_string_body",
    "_escape_control_chars_in_json_strings",
    "_escape_unescaped_quotes_in_json_strings",
    "_extract_and_close_truncated_tree",
    "_extract_json_object_value",
    "_extract_json_string_value",
    "_extract_malformed_quoted_field",
    "_extract_malformed_source",
    "_extract_truncated_string_value",
    "_extract_unterminated_json_string_field",
    "_find_json_string_end",
    "_format_code_execution_raw_args_error",
    "_format_tool_argument_blob_raw_args_error",
    "_is_json_string_terminator_after_quote",
    "_loads_json_dict",
    "_looks_like_stream_truncation",
    "_recover_canvas_publish_args",
    "_recover_code_execution_args",
    "_recover_emit_ui_patch_args",
    "_recover_emit_ui_tree_args",
    "_recover_project_apply_patch_args",
    "_recover_project_edit_args",
    "_recover_project_write_args",
    "_recover_tool_argument_blob_args",
    "_repair_trailing_commas",
    "_salvage_truncated_json_after_key",
    "_salvage_truncated_json_prefix",
    "_strip_json_code_fence",
    "_strip_json_prefix",
    "_tool_json_parse_recovery_hint",
    "_try_json_dict_raw_decode_trailing_junk",
    "_try_parse_closed_json_prefix",
    "_try_parse_raw_tool_args",
    "_try_repair_superfluous_closing_delimiter",
    "format_tool_arguments_json_error",
    "normalize_tool_parameters",
    "parse_tool_arguments_str",
    "strict_json_loads_error",
]
