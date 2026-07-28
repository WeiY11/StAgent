import { describe, expect, it } from 'vitest';
import { createMessagesSlice } from './messages-slice';
import type { ChatStore } from './types';
import type { Message } from '@/types/chat';

function makeStore(overrides: Partial<ChatStore> = {}) {
  let state = {
    sessions: [], currentSessionId: null, synced: false, chatSessionsReconciled: false,
    messages: {}, messagePages: {}, messagesLoadingSessionId: null,
    activeStreamSessionId: null, isLoading: false, isStreaming: false, error: null,
    streamAbortController: null, lastStopWasUserInitiated: false, lastTerminalReason: null,
    lastCheckpointId: null, pendingUserInput: null, nestedAgentPreviewBySession: {},
    sessionTodoUi: {}, codingProjectIdsBySession: {}, ...overrides,
  } as ChatStore;
  const set = (p: Partial<ChatStore> | ((s: ChatStore) => Partial<ChatStore>)) => {
    state = { ...state, ...(typeof p === 'function' ? p(state) : p) };
  };
  const get = () => state;
  const slice = createMessagesSlice(set, get, {} as any);
  state = { ...state, ...slice, ...overrides } as ChatStore;
  return { getState: get, actions: slice };
}

const msg = (id: string, role: 'user' | 'assistant' = 'user', content = 'hi'): Message =>
  ({ id, role, content, createdAt: '' }) as Message;

describe('messages-slice', () => {
  it('addMessage appends and updates session count', () => {
    const { actions, getState } = makeStore({
      sessions: [{ id: 's1', messageCount: 0 } as any],
    });
    actions.addMessage('s1', msg('m1'));
    expect(getState().messages['s1']).toHaveLength(1);
    expect(getState().sessions[0]!.messageCount).toBe(1);
  });

  it('updateMessage patches matching message', () => {
    const { actions, getState } = makeStore({ messages: { s1: [msg('m1'), msg('m2')] } });
    actions.updateMessage('s1', 'm1', { content: 'updated' });
    expect(getState().messages['s1']![0]!.content).toBe('updated');
    expect(getState().messages['s1']![1]!.content).toBe('hi');
  });

  it('appendToMessage concatenates content', () => {
    const { actions, getState } = makeStore({ messages: { s1: [msg('m1')] } });
    actions.appendToMessage('s1', 'm1', ' world');
    expect(getState().messages['s1']![0]!.content).toBe('hi world');
  });

  it('clearMessages resets messages and page state', () => {
    const { actions, getState } = makeStore({
      sessions: [{ id: 's1', messageCount: 3 } as any],
      messages: { s1: [msg('m1'), msg('m2')] },
      messagePages: { s1: { hasOlder: true, nextOlderPage: 3, isLoadingOlder: false } },
    });
    actions.clearMessages('s1');
    expect(getState().messages['s1']).toEqual([]);
    expect(getState().messagePages['s1']!.hasOlder).toBe(false);
    expect(getState().sessions[0]!.messageCount).toBe(0);
  });

  it('truncateAfterMessageId keeps up to target', () => {
    const { actions, getState } = makeStore({
      sessions: [{ id: 's1', messageCount: 3 } as any],
      messages: { s1: [msg('m1'), msg('m2'), msg('m3')] },
    });
    expect(actions.truncateAfterMessageId('s1', 'm2')).toBe(true);
    expect(getState().messages['s1']!).toHaveLength(2);
  });

  it('truncateAfterMessageId returns false when id missing', () => {
    const { actions } = makeStore({ messages: { s1: [msg('m1')] } });
    expect(actions.truncateAfterMessageId('s1', 'nope')).toBe(false);
  });

  it('getCurrentMessages returns messages for current session', () => {
    const { actions } = makeStore({ currentSessionId: 's1', messages: { s1: [msg('m1')] } });
    expect(actions.getCurrentMessages()).toHaveLength(1);
  });

  it('getCurrentMessages returns [] when no session', () => {
    const { actions } = makeStore();
    expect(actions.getCurrentMessages()).toEqual([]);
  });

  it('insertSteerMessage inserts before last assistant', () => {
    const { actions, getState } = makeStore({
      messages: { s1: [msg('m1', 'user'), msg('m2', 'assistant')] },
      sessions: [{ id: 's1', messageCount: 2 } as any],
    });
    actions.insertSteerMessage('s1', msg('steer', 'user', 'correction'));
    const msgs = getState().messages['s1']!;
    expect(msgs).toHaveLength(3);
    expect(msgs[1]!.id).toBe('steer');
  });

  it('finalizeToolCalls marks pending/running as terminal', () => {
    const { actions, getState } = makeStore({
      messages: {
        s1: [{ ...msg('m1'), toolCalls: [
          { id: 'tc1', name: 'x', status: 'pending' as const, arguments: {} },
          { id: 'tc2', name: 'y', status: 'success' as const, arguments: {} },
        ] } as any],
      },
    });
    actions.finalizeToolCalls('s1', 'm1', 'error');
    const tcs = getState().messages['s1']![0]!.toolCalls!;
    expect(tcs[0]!.status).toBe('error');
    expect(tcs[1]!.status).toBe('success'); // already terminal
  });
});
