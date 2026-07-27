/**
 * requestAnimationFrame-based batching for chat stream deltas.
 *
 * Accumulates text/tool-call chunks arriving from the SSE stream and flushes
 * them to the Zustand store once per animation frame, reducing DOM update
 * frequency and eliminating frame drops during fast token streams.
 */

import { useChatStore } from '@/stores/chat';

// ---------------------------------------------------------------------------
// Content delta batching
// ---------------------------------------------------------------------------
const _contentBatchBuffers = new Map<string, string>();
let _contentBatchRafId: number | null = null;

function _flushContentBatch(): void {
  _contentBatchRafId = null;
  const s = useChatStore.getState();
  for (const [key, text] of _contentBatchBuffers) {
    const [sessionId, msgId] = key.split('\0', 2);
    if (sessionId && msgId) {
      s.appendToMessage(sessionId, msgId, text);
    }
  }
  _contentBatchBuffers.clear();
}

export function batchContentDelta(sessionId: string, msgId: string, text: string): void {
  const key = `${sessionId}\0${msgId}`;
  const existing = _contentBatchBuffers.get(key);
  _contentBatchBuffers.set(key, existing ? existing + text : text);
  if (_contentBatchRafId === null) {
    _contentBatchRafId = requestAnimationFrame(_flushContentBatch);
  }
}

// ---------------------------------------------------------------------------
// Thinking delta batching
// ---------------------------------------------------------------------------
const _thinkingBatchBuffers = new Map<string, string>();
let _thinkingBatchRafId: number | null = null;

function _flushThinkingBatch(): void {
  _thinkingBatchRafId = null;
  const s = useChatStore.getState();
  for (const [key, text] of _thinkingBatchBuffers) {
    const [sessionId, msgId] = key.split('\0', 2);
    if (sessionId && msgId) {
      s.updateMessage(sessionId, msgId, { thinking: text });
    }
  }
  _thinkingBatchBuffers.clear();
}

export function batchThinkingDelta(sessionId: string, msgId: string, text: string): void {
  const key = `${sessionId}\0${msgId}`;
  _thinkingBatchBuffers.set(key, text);
  if (_thinkingBatchRafId === null) {
    _thinkingBatchRafId = requestAnimationFrame(_flushThinkingBatch);
  }
}

// ---------------------------------------------------------------------------
// Tool-call delta batching
// ---------------------------------------------------------------------------
export interface ToolDeltaSnapshot {
  sessionId: string;
  msgId: string;
  toolCallId: string;
  name: string;
  index: number;
  argumentsRaw: string;
  argumentsPartial: Record<string, unknown>;
}

const _toolDeltaBatchBuffers = new Map<string, ToolDeltaSnapshot>();
let _toolDeltaBatchRafId: number | null = null;

function _flushToolDeltaBatch(): void {
  _toolDeltaBatchRafId = null;
  const s = useChatStore.getState();
  for (const snapshot of _toolDeltaBatchBuffers.values()) {
    const prev =
      s.messages[snapshot.sessionId]?.find((m) => m.id === snapshot.msgId)?.toolCalls ?? [];
    const existingIdx = prev.findIndex((tc) => tc.id === snapshot.toolCallId);
    if (existingIdx === -1) {
      s.updateMessage(snapshot.sessionId, snapshot.msgId, {
        toolCalls: [
          ...prev,
          {
            id: snapshot.toolCallId,
            name: snapshot.name,
            arguments: snapshot.argumentsPartial,
            argumentsRaw: snapshot.argumentsRaw,
            toolCallIndex: snapshot.index,
            status: 'running',
          },
        ],
      });
    } else {
      s.updateToolCall(snapshot.sessionId, snapshot.msgId, snapshot.toolCallId, {
        argumentsRaw: snapshot.argumentsRaw,
        ...(Object.keys(snapshot.argumentsPartial).length > 0
          ? { arguments: snapshot.argumentsPartial }
          : {}),
        ...(snapshot.name !== 'unknown_tool' ? { name: snapshot.name } : {}),
        toolCallIndex: snapshot.index,
      });
    }
  }
  _toolDeltaBatchBuffers.clear();
}

export function batchToolCallDelta(snapshot: ToolDeltaSnapshot): void {
  const key = `${snapshot.sessionId}\0${snapshot.msgId}\0${snapshot.toolCallId}`;
  _toolDeltaBatchBuffers.set(key, snapshot);
  if (_toolDeltaBatchRafId === null) {
    _toolDeltaBatchRafId = requestAnimationFrame(_flushToolDeltaBatch);
  }
}

/**
 * Force-flush the tool-delta batch synchronously.  Called by the `tool_call`
 * event handler to ensure final tool-call state is committed before the
 * authoritative `tool_call` payload overwrites it.
 */
export function forceFlushToolDeltaBatch(): void {
  if (_toolDeltaBatchRafId !== null) {
    cancelAnimationFrame(_toolDeltaBatchRafId);
    _flushToolDeltaBatch();
  }
}

// ---------------------------------------------------------------------------
// Flush ALL pending batches (content + thinking + tool delta).
// ---------------------------------------------------------------------------

/**
 * Flush all pending rAF-batched deltas synchronously.
 * Call this when the stream ends or errors to ensure no buffered content is lost.
 */
export function flushPendingContentBatch(): void {
  if (_contentBatchRafId !== null) {
    cancelAnimationFrame(_contentBatchRafId);
    _flushContentBatch();
  }
  if (_thinkingBatchRafId !== null) {
    cancelAnimationFrame(_thinkingBatchRafId);
    _flushThinkingBatch();
  }
  if (_toolDeltaBatchRafId !== null) {
    cancelAnimationFrame(_toolDeltaBatchRafId);
    _flushToolDeltaBatch();
  }
}
