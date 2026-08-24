/**
 * Client-side mirror of the backend `GanttBuilder`.
 *
 * The REST snapshot seeds the chart; every subsequent WebSocket event is folded
 * in here so bars open and close without a refetch. The rules deliberately match
 * `StAgent/workflow/observability/gantt.py` — if the two ever disagree, the
 * server's chart is authoritative and a refetch repairs the view.
 */
import type { ExecutionGantt, GanttBar, GanttBarStatus } from '@/controllers/API/queries/observability';

export interface StreamEvent {
  type: string;
  prompt_id?: string;
  node_id?: string | null;
  state?: {
    node_id?: string;
    status?: string;
    value?: number;
    max?: number;
    metadata?: Record<string, unknown> | null;
    started_at_ms?: number | null;
    finished_at_ms?: number | null;
  } | null;
  data?: Record<string, unknown> | null;
}

interface NodeMeta {
  class_type?: string;
  display_name?: string;
}

/**
 * A chart plus the node catalogue that rode in on `execution_start`. The extra
 * field is client-only — the server never sends or reads it — and lets a bar
 * opened by a stream event carry the same label the server would have given it.
 */
export interface ClientGantt extends ExecutionGantt {
  node_meta?: Record<string, NodeMeta>;
}

/** Execution-level events that do *not* end the chart. */
const NON_TERMINAL_EXECUTION_EVENTS = new Set(['execution_start', 'execution_blocked']);

const CLOSING_EVENTS: Record<string, GanttBarStatus> = {
  node_success: 'success',
  node_error: 'error',
  executed: 'success',
};

const INSTANT_EVENTS: Record<string, GanttBarStatus> = {
  node_cached: 'cached',
  node_skipped: 'skipped',
};

export function emptyChart(promptId: string, now: number): ExecutionGantt {
  return {
    prompt_id: promptId,
    execution_id: null,
    flow_id: null,
    flow_name: null,
    started_at_ms: now,
    finished_at_ms: null,
    status: 'running',
    lane_count: 1,
    approximate: false,
    node_total: 0,
    bars: [],
    server_now_ms: now,
  };
}

/**
 * Greedy interval colouring, identical to `ExecutionGantt.assign_lanes`:
 * each bar takes the first lane whose last bar has already finished, so serial
 * dependencies share a row and parallel branches stagger into new ones.
 */
export function assignLanes(bars: GanttBar[]): { bars: GanttBar[]; laneCount: number } {
  const sorted = [...bars].sort(
    (a, b) =>
      a.started_at_ms - b.started_at_ms ||
      a.iteration - b.iteration ||
      a.node_id.localeCompare(b.node_id),
  );
  const laneEnds: number[] = [];
  for (const bar of sorted) {
    const end = bar.finished_at_ms ?? Number.POSITIVE_INFINITY;
    let placed = false;
    for (let lane = 0; lane < laneEnds.length; lane += 1) {
      if (laneEnds[lane] <= bar.started_at_ms) {
        bar.lane = lane;
        laneEnds[lane] = end;
        placed = true;
        break;
      }
    }
    if (!placed) {
      bar.lane = laneEnds.length;
      laneEnds.push(end);
    }
  }
  return { bars: sorted, laneCount: Math.max(1, laneEnds.length) };
}

function eventNodeId(event: StreamEvent): string | null {
  const id = event.node_id ?? event.state?.node_id ?? null;
  return id ? String(id) : null;
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function openBarFor(bars: GanttBar[], nodeId: string): GanttBar | undefined {
  // Last one wins: a loop re-entry appends, so the newest bar is the live one.
  for (let i = bars.length - 1; i >= 0; i -= 1) {
    if (bars[i].node_id === nodeId && bars[i].finished_at_ms === null) return bars[i];
  }
  return undefined;
}

function iterationFor(bars: GanttBar[], nodeId: string): number {
  return bars.filter((b) => b.node_id === nodeId).length;
}

function newBar(
  nodeId: string,
  startedAt: number,
  status: GanttBarStatus,
  iteration: number,
  meta: { class_type?: string; display_name?: string } = {},
): GanttBar {
  return {
    node_id: nodeId,
    class_type: meta.class_type ?? '',
    display_name: meta.display_name ?? nodeId,
    started_at_ms: startedAt,
    finished_at_ms: null,
    duration_ms: 0,
    status,
    lane: 0,
    cached: false,
    iteration,
    detail: {},
  };
}

function close(bar: GanttBar, at: number, status: GanttBarStatus): void {
  clamp(bar, at);
  // A bar that was blocked and closes successfully was resumed by a human.
  if (bar.status === 'blocked' && status === 'success') {
    bar.detail = { ...bar.detail, resumed: true };
  }
  bar.status = status;
}

/**
 * End a bar without reinterpreting it. Used when the execution finishes with
 * bars still open, and when a dropped close event is inferred — the status stays
 * whatever it was, matching `_on_execution_end` on the server.
 */
function clamp(bar: GanttBar, at: number): void {
  bar.finished_at_ms = Math.max(at, bar.started_at_ms);
  bar.duration_ms = bar.finished_at_ms - bar.started_at_ms;
}

/**
 * Fold one stream event into `chart`, returning a new chart (or the same
 * reference when the event is irrelevant, so React can skip the re-render).
 */
export function applyStreamEvent(
  chart: ClientGantt,
  event: StreamEvent,
  now: number,
): ClientGantt {
  const type = String(event.type ?? '');
  if (!type) return chart;

  if (type === 'execution_start') {
    const data = event.data ?? {};
    return {
      ...emptyChart(chart.prompt_id, num(data.started_at_ms) ?? now),
      execution_id: chart.execution_id,
      flow_id: (data.workflow_id as string) ?? chart.flow_id,
      flow_name: (data.flow_name as string) ?? chart.flow_name,
      node_total: num(data.node_total) ?? 0,
      node_meta: readCatalogue(data.nodes) ?? chart.node_meta,
    };
  }

  if (type.startsWith('execution_') && !NON_TERMINAL_EXECUTION_EVENTS.has(type)) {
    const end = num(event.data?.finished_at_ms) ?? now;
    // Clone every bar: `assignLanes` writes `lane`, and the previous chart is
    // still held by React until this one is committed.
    const bars = chart.bars.map((bar) => {
      const clone = { ...bar, detail: { ...bar.detail } };
      if (clone.finished_at_ms === null) clamp(clone, end);
      return clone;
    });
    const laid = assignLanes(bars);
    return {
      ...chart,
      finished_at_ms: end,
      status: type.slice('execution_'.length),
      bars: laid.bars,
      lane_count: laid.laneCount,
      server_now_ms: now,
    };
  }

  const nodeId = eventNodeId(event);
  if (!nodeId) return chart;

  const bars = chart.bars.map((b) => ({ ...b, detail: { ...b.detail } }));
  const stateStart = num(event.state?.started_at_ms);
  const stateEnd = num(event.state?.finished_at_ms);
  const open = openBarFor(bars, nodeId);
  let touched = false;

  if (type === 'executing') {
    const startedAt = stateStart ?? now;
    if (open && (stateStart === null || open.started_at_ms === startedAt)) {
      // Same span re-announced: a retry, not a second run.
      const attempt = (num(open.detail.retry) ?? 0) + 1;
      open.detail = { ...open.detail, retry: attempt };
      noteRetry(open, event.state);
    } else {
      if (open) clamp(open, startedAt);
      const fresh = newBar(
        nodeId,
        startedAt,
        'running',
        iterationFor(bars, nodeId),
        nodeMeta(chart, nodeId),
      );
      noteRetry(fresh, event.state);
      bars.push(fresh);
    }
    touched = true;
  } else if (type in INSTANT_EVENTS) {
    const status = INSTANT_EVENTS[type];
    const startedAt = stateStart ?? now;
    const bar =
      open ?? newBar(nodeId, startedAt, status, iterationFor(bars, nodeId), nodeMeta(chart, nodeId));
    if (!open) bars.push(bar);
    bar.cached = status === 'cached';
    close(bar, stateEnd ?? startedAt, status);
    touched = true;
  } else if (type === 'node_blocked') {
    const bar =
      open ??
      newBar(nodeId, stateStart ?? now, 'blocked', iterationFor(bars, nodeId), nodeMeta(chart, nodeId));
    if (!open) bars.push(bar);
    // Deliberately stays open: the hanging bar *is* the human-wait duration.
    bar.detail = { ...bar.detail, compute_ms: Math.max(0, (stateStart ?? now) - bar.started_at_ms) };
    bar.status = 'blocked';
    bar.finished_at_ms = null;
    touched = true;
  } else if (type in CLOSING_EVENTS) {
    if (open) {
      close(open, stateEnd ?? now, CLOSING_EVENTS[type]);
      const error = event.data?.error ?? event.state?.status;
      if (type === 'node_error' && typeof error === 'string') {
        open.detail = { ...open.detail, error };
      }
      touched = true;
    }
  } else if (type === 'progress' && open) {
    const value = num(event.state?.value);
    const max = num(event.state?.max) || 1;
    if (value !== null) {
      open.detail = { ...open.detail, progress: value / max };
      touched = true;
    }
  }

  if (!touched) return chart;
  const laid = assignLanes(bars);
  return { ...chart, bars: laid.bars, lane_count: laid.laneCount, server_now_ms: now };
}

/**
 * Prefer the engine's own retry counters when the event carries them. The server's
 * `_note_retry` reads exactly this metadata, so a later refetch agrees with the
 * streamed chart instead of contradicting a locally-counted guess.
 */
function noteRetry(bar: GanttBar, state: StreamEvent['state']): void {
  const meta = state?.metadata;
  if (!meta || typeof meta !== 'object') return;
  const retry = num(meta.retry);
  const maxRetries = num(meta.max_retries);
  if (retry !== null) bar.detail = { ...bar.detail, retry };
  if (maxRetries !== null) bar.detail = { ...bar.detail, max_retries: maxRetries };
}

/** Node labels, preferring the `execution_start` catalogue over an earlier bar. */
function nodeMeta(chart: ClientGantt, nodeId: string): NodeMeta {
  const catalogued = chart.node_meta?.[nodeId];
  if (catalogued) return catalogued;
  const previous = chart.bars.find((b) => b.node_id === nodeId);
  return previous
    ? { class_type: previous.class_type, display_name: previous.display_name }
    : {};
}

/** Read `_node_catalogue`'s `{node_id: {class_type, display_name}}` payload. */
function readCatalogue(raw: unknown): Record<string, NodeMeta> | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const out: Record<string, NodeMeta> = {};
  for (const [nodeId, spec] of Object.entries(raw as Record<string, unknown>)) {
    if (!spec || typeof spec !== 'object') continue;
    const entry = spec as Record<string, unknown>;
    out[nodeId] = {
      class_type: typeof entry.class_type === 'string' ? entry.class_type : undefined,
      display_name: typeof entry.display_name === 'string' ? entry.display_name : undefined,
    };
  }
  return Object.keys(out).length > 0 ? out : null;
}
