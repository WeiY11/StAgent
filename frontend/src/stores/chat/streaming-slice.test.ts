import { describe, expect, it } from 'vitest';
import type { PendingUserInput } from '@/types/chat';
import { createStreamingSlice } from './streaming-slice';
import type { ChatStore } from './types';

function makeStore(overrides: Partial<ChatStore> = {}) {
  let state = {
    ...overrides,
    sessions: [],
    currentSessionId: null,
    synced: false,
    chatSessionsReconciled: false,
    messages: {},
    messagePages: {},
    messagesLoadingSessionId: null,
    activeStreamSessionId: null,
    isLoading: false,
    isStreaming: false,
    error: null,
    streamAbortController: null,
    lastStopWasUserInitiated: false,
    lastTerminalReason: null,
    lastCheckpointId: null,
    pendingUserInput: null,
    nestedAgentPreviewBySession: {},
    sessionTodoUi: {},
    codingProjectIdsBySession: {},
  } as ChatStore;
  const set = (partial: Partial<ChatStore> | ((s: ChatStore) => Partial<ChatStore>)) => {
    const patch = typeof partial === 'function' ? partial(state) : partial;
    state = { ...state, ...patch };
  };
  const get = () => state;
  const slice = createStreamingSlice(
    set,
    get,
    {} as Parameters<typeof createStreamingSlice>[2],
  );
  state = { ...state, ...slice, ...overrides } as ChatStore;
  return { getState: get, actions: slice };
}

describe('streaming-slice', () => {
  it('has correct initial state', () => {
    const { getState } = makeStore();
    const s = getState();
    expect(s.activeStreamSessionId).toBeNull();
    expect(s.isLoading).toBe(false);
    expect(s.isStreaming).toBe(false);
    expect(s.error).toBeNull();
    expect(s.pendingUserInput).toBeNull();
  });

  it('setLoading / setStreaming toggle flags', () => {
    const { actions, getState } = makeStore();
    actions.setLoading(true);
    expect(getState().isLoading).toBe(true);
    actions.setStreaming(true);
    expect(getState().isStreaming).toBe(true);
  });

  it('beginChatStreamSession claims session and sets loading+streaming', () => {
    const { actions, getState } = makeStore();
    actions.beginChatStreamSession('s1');
    const s = getState();
    expect(s.activeStreamSessionId).toBe('s1');
    expect(s.isLoading).toBe(true);
    expect(s.isStreaming).toBe(true);
  });

  it('releaseChatStreamSession only releases owning session', () => {
    const { actions, getState } = makeStore();
    actions.beginChatStreamSession('s1');
    actions.releaseChatStreamSession('s2'); // different session — no-op
    expect(getState().activeStreamSessionId).toBe('s1');
    actions.releaseChatStreamSession('s1');
    expect(getState().activeStreamSessionId).toBeNull();
    expect(getState().isLoading).toBe(false);
  });

  it('abortActiveStreamUnlessSession aborts when different session', () => {
    const ctrl = new AbortController();
    const { actions } = makeStore({ activeStreamSessionId: 's1', streamAbortController: ctrl });
    actions.abortActiveStreamUnlessSession('s2');
    expect(ctrl.signal.aborted).toBe(true);
  });

  it('abortActiveStreamUnlessSession does NOT abort for same session', () => {
    const ctrl = new AbortController();
    const { actions } = makeStore({ activeStreamSessionId: 's1', streamAbortController: ctrl });
    actions.abortActiveStreamUnlessSession('s1');
    expect(ctrl.signal.aborted).toBe(false);
  });

  it('setError / setPendingUserInput / clearPendingUserInput', () => {
    const { actions, getState } = makeStore();
    actions.setError('oops');
    expect(getState().error).toBe('oops');
    const payload: PendingUserInput = {
      sessionId: 's1',
      assistantMsgId: 'm1',
      toolCallId: 'tc1',
      questions: [],
    };
    actions.setPendingUserInput(payload);
    expect(getState().pendingUserInput).toBe(payload);
    actions.clearPendingUserInput();
    expect(getState().pendingUserInput).toBeNull();
  });

  it('setStreamAbortController / releaseStreamAbortController', () => {
    const { actions, getState } = makeStore();
    const ctrl = new AbortController();
    actions.setStreamAbortController(ctrl);
    expect(getState().streamAbortController).toBe(ctrl);
    actions.releaseStreamAbortController(ctrl);
    expect(getState().streamAbortController).toBeNull();
  });

  it('releaseStreamAbortController ignores stale instance', () => {
    const ctrl1 = new AbortController();
    const ctrl2 = new AbortController();
    const { actions, getState } = makeStore({ streamAbortController: ctrl1 });
    actions.releaseStreamAbortController(ctrl2);
    expect(getState().streamAbortController).toBe(ctrl1);
  });
});
