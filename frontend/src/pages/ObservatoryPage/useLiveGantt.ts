/**
 * Live Gantt state for one execution.
 *
 * Seeds from `GET /observability/gantt/{prompt_id}` and then folds the
 * `WS /observability/stream/{prompt_id}` events in locally, so bars open, grow
 * and close per event instead of per poll. Sockets are ref-counted per prompt id
 * so several components (page + drawer + card thumbnail) share one connection.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { openAuthedWebSocket } from '@/lib/auth/authedTransport';
import { URL_KEYS } from '@/controllers/API/helpers/constants';
import {
  useExecutionGantt,
  type ExecutionGantt,
} from '@/controllers/API/queries/observability';
import { applyStreamEvent, type ClientGantt, type StreamEvent } from './ganttReducer';

type Listener = (event: StreamEvent) => void;

const listeners = new Map<string, Set<Listener>>();
const sockets = new Map<string, WebSocket>();

function wsUrl(promptId: string): string {
  const base = import.meta.env.VITE_API_BASE_URL || '/api/v1';
  const url = new URL(`${base}${URL_KEYS.OBS_STREAM(promptId)}`, window.location.origin);
  url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

function fanout(promptId: string, raw: string): void {
  let event: StreamEvent;
  try {
    event = JSON.parse(raw) as StreamEvent;
  } catch {
    return;
  }
  for (const listener of listeners.get(promptId) ?? []) {
    try {
      listener(event);
    } catch {
      /* One broken subscriber must not silence the rest. */
    }
  }
}

function subscribe(promptId: string, listener: Listener): () => void {
  let set = listeners.get(promptId);
  if (!set) {
    set = new Set();
    listeners.set(promptId, set);
  }
  set.add(listener);

  if (!sockets.has(promptId)) {
    const socket = openAuthedWebSocket(wsUrl(promptId));
    sockets.set(promptId, socket);
    socket.onmessage = (event) => fanout(promptId, event.data as string);
    socket.onclose = () => {
      if (sockets.get(promptId) === socket) sockets.delete(promptId);
    };
    socket.onerror = () => {
      /* The REST snapshot remains the source of truth if the socket never opens. */
    };
  }

  return () => {
    const current = listeners.get(promptId);
    current?.delete(listener);
    if (current && current.size === 0) {
      listeners.delete(promptId);
      sockets.get(promptId)?.close();
      sockets.delete(promptId);
    }
  };
}

/** Test helper — drop every shared socket and subscriber. */
export function resetGanttStreams(): void {
  for (const socket of sockets.values()) socket.close();
  sockets.clear();
  listeners.clear();
}

export interface LiveGanttResult {
  chart: ClientGantt | null;
  /** Server-aligned clock, so open bars grow against the same origin as the bars. */
  nowMs: number;
  isLoading: boolean;
  isError: boolean;
  /** True while at least one bar is still open. */
  isLive: boolean;
  refetch: () => void;
}

const TICK_MS = 200;

export function useLiveGantt(
  promptId: string | null | undefined,
  options?: { enabled?: boolean; seed?: ExecutionGantt | null },
): LiveGanttResult {
  const enabled = options?.enabled !== false && !!promptId;
  const query = useExecutionGantt(promptId, { enabled: enabled && !options?.seed });
  const [chart, setChart] = useState<ClientGantt | null>(options?.seed ?? null);
  const skewRef = useRef(0);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const snapshot = options?.seed ?? query.data ?? null;

  // Re-seed whenever the server hands us a fresh chart for a different run.
  useEffect(() => {
    if (!snapshot) return;
    skewRef.current = snapshot.server_now_ms - Date.now();
    setChart((current) =>
      current && current.prompt_id === snapshot.prompt_id && current.bars.length > snapshot.bars.length
        ? current // keep locally-streamed detail the snapshot has not caught up with
        : // The catalogue only ever arrives on the stream, so carry it across re-seeds.
          { ...snapshot, node_meta: current?.node_meta },
    );
  }, [snapshot]);

  useEffect(() => {
    if (!enabled || !promptId) return;
    return subscribe(promptId, (event) => {
      setChart((current) =>
        applyStreamEvent(current ?? { ...emptySeed(promptId) }, event, Date.now() + skewRef.current),
      );
    });
  }, [enabled, promptId]);

  const isLive = useMemo(
    () => !!chart && (chart.finished_at_ms === null || chart.bars.some((b) => b.finished_at_ms === null)),
    [chart],
  );

  // Only tick while something is actually growing.
  useEffect(() => {
    if (!isLive) return;
    const id = window.setInterval(() => setNowMs(Date.now() + skewRef.current), TICK_MS);
    return () => window.clearInterval(id);
  }, [isLive]);

  const refetch = useCallback(() => {
    void query.refetch();
  }, [query]);

  return {
    chart,
    nowMs: isLive ? nowMs : (chart?.finished_at_ms ?? nowMs),
    isLoading: query.isLoading && !chart,
    isError: query.isError && !chart,
    isLive,
    refetch,
  };
}

function emptySeed(promptId: string): ExecutionGantt {
  return {
    prompt_id: promptId,
    execution_id: null,
    flow_id: null,
    flow_name: null,
    started_at_ms: Date.now(),
    finished_at_ms: null,
    status: 'running',
    lane_count: 1,
    approximate: false,
    node_total: 0,
    bars: [],
    server_now_ms: Date.now(),
  };
}
