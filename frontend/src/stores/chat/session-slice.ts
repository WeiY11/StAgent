import type { StateCreator } from 'zustand';
import i18n from '@/i18n';
import { apiClient, HttpError } from '@/api/client';
import { generateId, isUuid } from '@/lib/utils/utils';
import { queryClient } from '@/lib/utils/queryClient';
import type { ChatSession } from '@/types/chat';
import { useArtifactStore } from '../artifact';
import { useChatDraftStore } from '../chatDraft';
import { useLayoutStore } from '../layout';
import { useGenUiStore } from '../genUi';
import { useExecutionSessionStore } from '../executionSession';
import { getChatProjectHeaders, useChatProjectStore } from '../chatProjects';
import {
  isChatStreamBusyForSession,
  mapSession,
  type PaginatedResponse,
  type SessionResponse,
} from '../chatHelpers';
import type { ChatStore, SessionSliceState, SessionSliceActions } from './types';

/** Resolve the project id for a session from the current store state. */
function projectIdForSession(get: () => ChatStore, sessionId: string): string | null {
  const session = get().sessions.find((s) => s.id === sessionId);
  return session?.projectId ?? null;
}

export const createSessionSlice: StateCreator<
  ChatStore,
  [['zustand/persist', ChatStore]],
  [],
  SessionSliceState & SessionSliceActions
> = (set, get) => ({
  // ---- state ----
  sessions: [],
  currentSessionId: null,
  synced: false,
  chatSessionsReconciled: false,

  // ---- actions ----

  fetchSessions: async (projectId) => {
    try {
      const params: Record<string, string | number> = { page: 1, page_size: 100 };
      if (projectId) params.project_id = projectId;
      const res = await apiClient.get<PaginatedResponse<SessionResponse>>(
        '/chat/sessions',
        params,
        { headers: getChatProjectHeaders(projectId) },
      );
      const sessions = res.items.map(mapSession);
      set((state) => ({
        sessions,
        synced: true,
        currentSessionId: state.currentSessionId && sessions.some((s) => s.id === state.currentSessionId)
          ? state.currentSessionId
          : sessions[0]?.id ?? null,
      }));
    } catch {
      // Fall back to local sessions if backend is unreachable
    } finally {
      set({ chatSessionsReconciled: true });
    }
  },

  createSession: async (title, projectIdArg) => {
    const activeProjectId =
      projectIdArg !== undefined
        ? projectIdArg
        : useChatProjectStore.getState().currentProjectId;
    const tempId = generateId();
    const now = new Date().toISOString();
    const tempSession: ChatSession = {
      id: tempId,
      title: title || i18n.t('chat.defaultSessionName'),
      createdAt: now,
      updatedAt: now,
      messageCount: 0,
      projectId: activeProjectId ?? null,
      pinnedMessageIds: [],
      isPending: true,
    };

    set((state) => ({
      sessions: [tempSession, ...state.sessions],
      currentSessionId: tempId,
      messages: { ...state.messages, [tempId]: [] },
      sessionTodoUi: {
        ...state.sessionTodoUi,
        [tempId]: { pinned: false, dismissed: false },
      },
      pendingUserInput: null,
    }));
    useExecutionSessionStore.getState().clearSession(tempId);

    try {
      const res = await apiClient.post<SessionResponse>('/chat/sessions', {
        name: title || i18n.t('chat.defaultSessionName'),
        project_id: activeProjectId || undefined,
      }, { headers: getChatProjectHeaders(activeProjectId) });
      const session = mapSession(res);
      delete session.todos;

      set((state) => {
        const newMessages = { ...state.messages };
        const tempMessages = newMessages[tempId] || [];
        delete newMessages[tempId];
        newMessages[session.id] = tempMessages;

        const nextTodoUi = { ...state.sessionTodoUi };
        delete nextTodoUi[tempId];
        nextTodoUi[session.id] = { pinned: false, dismissed: false };

        return {
          sessions: state.sessions.map((s) => (s.id === tempId ? session : s)),
          currentSessionId: state.currentSessionId === tempId ? session.id : state.currentSessionId,
          messages: newMessages,
          sessionTodoUi: nextTodoUi,
          pendingUserInput:
            state.pendingUserInput &&
            (state.pendingUserInput.sessionId === tempId ||
              state.pendingUserInput.sessionId === session.id)
              ? null
              : state.pendingUserInput,
        };
      });
      useExecutionSessionStore.getState().remapSession(tempId, session.id);

      return session.id;
    } catch {
      return tempId;
    }
  },

  deleteSession: async (id) => {
    const state = get();
    if (isChatStreamBusyForSession(id, state) && state.currentSessionId === id) {
      get().abortChatStreamFromUser();
    }

    const wasCurrentSession = state.currentSessionId === id;
    set((state) => {
      const newSessions = state.sessions.filter((s) => s.id !== id);
      const newMessages = { ...state.messages };
      const newMessagePages = { ...state.messagePages };
      delete newMessages[id];
      delete newMessagePages[id];
      const clearPending =
        state.pendingUserInput?.sessionId === id ? null : state.pendingUserInput;
      return {
        sessions: newSessions,
        currentSessionId: state.currentSessionId === id
          ? newSessions[0]?.id ?? null
          : state.currentSessionId,
        messages: newMessages,
        messagePages: newMessagePages,
        pendingUserInput: clearPending,
      };
    });

    // Reset workspace-side state when deleting the active session.
    if (wasCurrentSession) {
      useChatDraftStore.getState().clearPendingInsert();
      useChatDraftStore.getState().clearFolderContext();
      useLayoutStore.setState({ workspaceTab: 'files' });
      useArtifactStore.getState().closeArtifact();
      useArtifactStore.setState({ openTabIds: [], activeTabId: null });
    }
    useArtifactStore.getState().clearSessionArtifacts(id);
    useGenUiStore.getState().clearForSession(id);
    useExecutionSessionStore.getState().clearSession(id);

    try {
      await apiClient.delete(`/chat/sessions/${id}`, {
        headers: getChatProjectHeaders(projectIdForSession(get, id)),
      });
    } catch {
      // Session already removed from local state
    }
  },

  dropStaleSession: (id) => {
    const wasCurrentSession = get().currentSessionId === id;
    set((state) => {
      const newSessions = state.sessions.filter((s) => s.id !== id);
      const newMessages = { ...state.messages };
      const newMessagePages = { ...state.messagePages };
      delete newMessages[id];
      delete newMessagePages[id];
      const clearPending =
        state.pendingUserInput?.sessionId === id ? null : state.pendingUserInput;
      return {
        sessions: newSessions,
        currentSessionId: state.currentSessionId === id
          ? newSessions[0]?.id ?? null
          : state.currentSessionId,
        messages: newMessages,
        messagePages: newMessagePages,
        pendingUserInput: clearPending,
      };
    });

    if (wasCurrentSession) {
      useChatDraftStore.getState().clearPendingInsert();
      useChatDraftStore.getState().clearFolderContext();
      useLayoutStore.setState({ workspaceTab: 'files' });
      useArtifactStore.getState().closeArtifact();
      useArtifactStore.setState({ openTabIds: [], activeTabId: null });
    }
    useArtifactStore.getState().clearSessionArtifacts(id);
    useGenUiStore.getState().clearForSession(id);
  },

  selectSession: (id) => {
    if (!isUuid(id)) return;
    set((state) => ({
      currentSessionId: id,
      pendingUserInput:
        state.pendingUserInput && state.pendingUserInput.sessionId !== id
          ? null
          : state.pendingUserInput,
    }));
    void queryClient.invalidateQueries({ queryKey: ['prompt-preview', id] });
    void get().fetchMessages(id);
    void get().fetchSessionDetail(id);
  },

  forkSession: async (sessionId, upToMessageId) => {
    if (!isUuid(sessionId) || !isUuid(upToMessageId)) return null;
    try {
      const res = await apiClient.post<{ session_id: string; message_count: number }>(
        `/chat/sessions/${sessionId}/fork`,
        { up_to_message_id: upToMessageId },
        { headers: getChatProjectHeaders(projectIdForSession(get, sessionId)) },
      );
      const forkedId = res.session_id;
      await get().fetchSessions();
      get().selectSession(forkedId);
      return forkedId;
    } catch {
      return null;
    }
  },

  updateSessionTitle: (id, title) => {
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id ? { ...s, title, updatedAt: new Date().toISOString() } : s
      ),
    }));
    apiClient.patch(
      `/chat/sessions/${id}`,
      { name: title },
      { headers: getChatProjectHeaders(projectIdForSession(get, id)) },
    ).catch(() => {});
  },

  fetchSessionDetail: async (sessionId) => {
    if (!isUuid(sessionId)) return;
    try {
      const res = await apiClient.get<SessionResponse>(
        `/chat/sessions/${sessionId}`,
        undefined,
        { headers: getChatProjectHeaders(projectIdForSession(get, sessionId)) },
      );
      const mapped = mapSession(res);
      set((state) => ({
        sessions: state.sessions.map((s) => {
          if (s.id !== sessionId) return s;
          const streaming = isChatStreamBusyForSession(sessionId, state);
          const next: ChatSession = { ...s, ...mapped };
          if (mapped.todos?.length) {
            next.todos = mapped.todos;
          } else if (Array.isArray(res.todos) && !streaming) {
            delete next.todos;
          } else {
            next.todos = s.todos;
          }
          return next;
        }),
      }));
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) {
        get().dropStaleSession(sessionId);
      }
    }
  },

  getCurrentSession: () => {
    const state = get();
    if (!state.currentSessionId) return null;
    return state.sessions.find((s) => s.id === state.currentSessionId) || null;
  },
});
