import type { StateCreator } from 'zustand';
import { apiClient } from '@/api/client';
import { getChatProjectHeaders } from '../chatProjects';
import type { ChatStore, StreamingSliceState, StreamingSliceActions } from './types';

/** Resolve the project id for a session from the current store state. */
function projectIdForSession(get: () => ChatStore, sessionId: string): string | null {
  const session = get().sessions.find((s) => s.id === sessionId);
  return session?.projectId ?? null;
}

export const createStreamingSlice: StateCreator<
  ChatStore,
  [['zustand/persist', ChatStore]],
  [],
  StreamingSliceState & StreamingSliceActions
> = (set, get) => ({
  // ---- state ----
  activeStreamSessionId: null,
  isLoading: false,
  isStreaming: false,
  error: null,
  streamAbortController: null,
  lastStopWasUserInitiated: false,
  lastTerminalReason: null,
  lastCheckpointId: null,
  pendingUserInput: null,

  // ---- actions ----

  setLoading: (isLoading) => set({ isLoading }),
  setStreaming: (isStreaming) => set({ isStreaming }),

  beginChatStreamSession: (sessionId) =>
    set({
      activeStreamSessionId: sessionId,
      isLoading: true,
      isStreaming: true,
    }),

  releaseChatStreamSession: (sessionId) => {
    const s = get();
    if (s.activeStreamSessionId !== sessionId) return;
    set({
      activeStreamSessionId: null,
      isLoading: false,
      isStreaming: false,
    });
  },

  releaseChatStreamSessionAndResync: (sessionId) => {
    get().releaseChatStreamSession(sessionId);
    void get().fetchMessages(sessionId);
    void get().fetchSessionDetail(sessionId);
  },

  abortActiveStreamUnlessSession: (sessionId) => {
    const s = get();
    if (
      s.streamAbortController &&
      s.activeStreamSessionId != null &&
      s.activeStreamSessionId !== sessionId
    ) {
      s.streamAbortController.abort();
    }
  },

  setError: (error) => set({ error }),
  setPendingUserInput: (payload) => set({ pendingUserInput: payload }),
  clearPendingUserInput: () => set({ pendingUserInput: null }),

  setStreamAbortController: (controller) => set({ streamAbortController: controller }),

  releaseStreamAbortController: (instance) => {
    const cur = get().streamAbortController;
    if (cur === instance) set({ streamAbortController: null });
  },

  abortChatStreamFromUser: () => {
    get().streamAbortController?.abort();
    set({ lastStopWasUserInitiated: true });

    const sessionId = get().currentSessionId;
    if (sessionId) {
      void get().cancelBackendSession(sessionId);
    }
  },

  cancelBackendSession: async (sessionId: string) => {
    try {
      const res = await apiClient.post<{
        session_id: string;
        cancelled: boolean;
        checkpoint_id?: string | null;
        message?: string;
      }>(
        `/chat/sessions/${sessionId}/cancel`,
        undefined,
        { headers: getChatProjectHeaders(projectIdForSession(get, sessionId)) },
      );
      const checkpointId =
        typeof res?.checkpoint_id === 'string' && res.checkpoint_id.trim()
          ? res.checkpoint_id.trim()
          : null;
      if (checkpointId) {
        set({
          lastCheckpointId: checkpointId,
          lastTerminalReason: 'aborted_streaming',
        });
      } else if (get().lastStopWasUserInitiated) {
        // Still surface the Stop banner even if the checkpoint is late;
        // Continue stays disabled until a checkpoint id arrives via SSE.
        set({ lastTerminalReason: 'aborted_streaming' });
      }
    } catch {
      // Best-effort; the SSE abort already closed the client connection
      if (get().lastStopWasUserInitiated) {
        set({ lastTerminalReason: 'aborted_streaming' });
      }
    }
  },
});
