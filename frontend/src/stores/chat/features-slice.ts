import type { StateCreator } from 'zustand';
import { isUuid } from '@/lib/utils/utils';
import { apiClient } from '@/api/client';
import type { TaskProgressStep } from '@/types/chat';
import { getChatProjectHeaders } from '../chatProjects';
import {
  applyTodoStatusUpdate,
  findLatestTodoAnchorMessage,
  mapApiTodosToSteps,
  mapSession,
  TASK_STATUS_RANK,
  type SessionResponse,
} from '../chatHelpers';
import type { ChatStore, FeaturesSliceState, FeaturesSliceActions } from './types';

/** Resolve the project id for a session from the current store state. */
function projectIdForSession(get: () => ChatStore, sessionId: string): string | null {
  const session = get().sessions.find((s) => s.id === sessionId);
  return session?.projectId ?? null;
}

function resolveSessionTodoBaseline(
  sessionId: string,
  state: Pick<ChatStore, 'sessions' | 'messages'>,
): TaskProgressStep[] {
  const fromSession = state.sessions.find((s) => s.id === sessionId)?.todos;
  if (fromSession?.length) return fromSession;
  return findLatestTodoAnchorMessage(state.messages[sessionId] ?? [])?.taskProgress ?? [];
}

function syncTodoAnchorMessageProgress(
  sessionId: string,
  todos: TaskProgressStep[],
  set: (partial: Partial<ChatStore> | ((state: ChatStore) => Partial<ChatStore>)) => void,
  get: () => ChatStore,
) {
  const anchor = findLatestTodoAnchorMessage(get().messages[sessionId] ?? []);
  if (!anchor) return;
  set((state) => ({
    messages: {
      ...state.messages,
      [sessionId]: (state.messages[sessionId] || []).map((m) =>
        m.id === anchor.id ? { ...m, taskProgress: todos } : m,
      ),
    },
  }));
}

export const createFeaturesSlice: StateCreator<
  ChatStore,
  [['zustand/persist', ChatStore]],
  [],
  FeaturesSliceState & FeaturesSliceActions
> = (set, get) => ({
  // ---- state ----
  nestedAgentPreviewBySession: {},
  sessionTodoUi: {},
  codingProjectIdsBySession: {},

  // ---- actions ----

  setNestedAgentPreview: (sessionId, preview) => {
    set((state) => ({
      nestedAgentPreviewBySession: {
        ...state.nestedAgentPreviewBySession,
        [sessionId]: preview,
      },
    }));
  },

  setSessionTodos: (sessionId, todos) => {
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === sessionId ? { ...s, todos: todos.length ? todos : undefined } : s,
      ),
    }));
    if (todos.length) {
      syncTodoAnchorMessageProgress(sessionId, todos, set, get);
    }
  },

  setSessionTodoPinned: (sessionId, pinned) => {
    set((state) => ({
      sessionTodoUi: {
        ...state.sessionTodoUi,
        [sessionId]: {
          pinned,
          dismissed: pinned ? false : (state.sessionTodoUi[sessionId]?.dismissed ?? false),
        },
      },
    }));
  },

  dismissSessionTodoPanel: (sessionId) => {
    set((state) => ({
      sessionTodoUi: {
        ...state.sessionTodoUi,
        [sessionId]: {
          pinned: state.sessionTodoUi[sessionId]?.pinned ?? true,
          dismissed: true,
        },
      },
    }));
  },

  upsertSessionTodoFromProgress: (sessionId, step) => {
    set((state) => ({
      sessions: state.sessions.map((s) => {
        if (s.id !== sessionId) return s;
        const existing = s.todos ?? [];
        const idx = existing.findIndex((item) => item.taskId === step.taskId);
        if (idx === -1) {
          return { ...s, todos: [...existing, step] };
        }
        const prev = existing[idx];
        if (!prev) return s;
        const prevRank = TASK_STATUS_RANK[prev.status];
        const nextRank = TASK_STATUS_RANK[step.status];
        if (nextRank < prevRank) return s;
        if (
          nextRank === prevRank &&
          prev.status === step.status &&
          prev.label === step.label &&
          prev.progress === step.progress &&
          prev.order === step.order
        ) {
          return s;
        }
        const next = [...existing];
        next[idx] = {
          ...prev,
          ...step,
          order: step.order ?? prev.order,
        };
        return { ...s, todos: next };
      }),
    }));
    const todos = get().sessions.find((s) => s.id === sessionId)?.todos;
    if (todos?.length) {
      syncTodoAnchorMessageProgress(sessionId, todos, set, get);
    }
  },

  patchSessionTodoStatus: async (sessionId, taskId, status) => {
    if (!isUuid(sessionId)) return;
    const state = get();
    const prev = resolveSessionTodoBaseline(sessionId, state);
    if (!prev.length) return;

    const optimistic = applyTodoStatusUpdate(prev, taskId, status);
    get().setSessionTodos(sessionId, optimistic);
    syncTodoAnchorMessageProgress(sessionId, optimistic, set, get);

    try {
      const res = await apiClient.patch<SessionResponse>(
        `/chat/sessions/${sessionId}/todos/${encodeURIComponent(taskId)}`,
        { status },
        { headers: getChatProjectHeaders(projectIdForSession(get, sessionId)) },
      );
      const mapped = mapApiTodosToSteps(res.todos);
      if (mapped?.length) {
        get().setSessionTodos(sessionId, mapped);
        syncTodoAnchorMessageProgress(sessionId, mapped, set, get);
      }
    } catch {
      get().setSessionTodos(sessionId, prev);
      syncTodoAnchorMessageProgress(sessionId, prev, set, get);
    }
  },

  setPinnedMessageIds: async (sessionId, messageIds) => {
    const prev = get().sessions.find((s) => s.id === sessionId);
    const previousPins = prev?.pinnedMessageIds ?? [];
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === sessionId ? { ...s, pinnedMessageIds: messageIds } : s,
      ),
    }));
    try {
      const res = await apiClient.patch<SessionResponse>(
        `/chat/sessions/${sessionId}`,
        {
          metadata_patch: { pinned_message_ids: messageIds },
        },
        { headers: getChatProjectHeaders(projectIdForSession(get, sessionId)) },
      );
      const mapped = mapSession(res);
      set((state) => ({
        sessions: state.sessions.map((s) => (s.id === sessionId ? { ...s, ...mapped } : s)),
      }));
    } catch {
      set((state) => ({
        sessions: state.sessions.map((s) =>
          s.id === sessionId ? { ...s, pinnedMessageIds: previousPins } : s,
        ),
      }));
    }
  },

  togglePinMessage: async (sessionId, messageId) => {
    const sess = get().sessions.find((s) => s.id === sessionId);
    const cur = sess?.pinnedMessageIds ?? [];
    const has = cur.includes(messageId);
    const next = has ? cur.filter((id) => id !== messageId) : [...cur, messageId];
    await get().setPinnedMessageIds(sessionId, next);
  },

  registerCodingProjectForSession: (sessionId, projectId) => {
    const id = projectId.trim();
    if (!sessionId || !id) return;
    set((state) => {
      const cur = state.codingProjectIdsBySession[sessionId] ?? [];
      if (cur.includes(id)) return state;
      return {
        codingProjectIdsBySession: {
          ...state.codingProjectIdsBySession,
          [sessionId]: [...cur, id],
        },
      };
    });
  },

  stopCodingProjectsForSession: async (sessionId) => {
    if (!sessionId) return;
    const ids = get().codingProjectIdsBySession[sessionId] ?? [];
    set((s) => {
      const next = { ...s.codingProjectIdsBySession };
      delete next[sessionId];
      return { codingProjectIdsBySession: next };
    });
    for (const projectId of ids) {
      try {
        await apiClient.post(`/coding-projects/${projectId}/stop`);
      } catch {
        /* best-effort */
      }
    }
  },
});
