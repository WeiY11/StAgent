import type { StateCreator } from 'zustand';
import { isUuid } from '@/lib/utils/utils';
import { apiClient, HttpError } from '@/api/client';
import {
  ensureChronologicalMessages,
  normalizeMessageList,
  type MessageResponse,
} from '@/types/chatHistory';
import { enrichMessagesWithSessionAttachments } from '@/lib/chat/chatAttachments';
import { hydrateSessionCanvasArtifacts } from '../artifact';
import { hydrateGenUiFromMessages, useGenUiStore } from '../genUi';
import { useArtifactStore } from '../artifact';
import { getChatProjectHeaders } from '../chatProjects';
import {
  CHAT_MESSAGES_PAGE_SIZE,
  dedupeMessagesByIdPreserveOrder,
  isChatStreamBusyForSession,
  TASK_STATUS_RANK,
  type PaginatedResponse,
} from '../chatHelpers';
import type { ChatStore, MessagesSliceState, MessagesSliceActions } from './types';

/** Skip server fetch while the HTTP stream for this session is still active. */
function shouldSkipFetchMessages(sessionId: string, state: ChatStore): boolean {
  return isChatStreamBusyForSession(sessionId, state);
}

/** Resolve the project id for a session from the current store state. */
function projectIdForSession(get: () => ChatStore, sessionId: string): string | null {
  const session = get().sessions.find((s) => s.id === sessionId);
  return session?.projectId ?? null;
}

/** Invalidates in-flight GET /messages responses when switching threads quickly. */
let messagesFetchSeq = 0;

export const createMessagesSlice: StateCreator<
  ChatStore,
  [['zustand/persist', ChatStore]],
  [],
  MessagesSliceState & MessagesSliceActions
> = (set, get) => ({
  // ---- state ----
  messages: {},
  messagePages: {},
  messagesLoadingSessionId: null,

  // ---- actions ----

  fetchMessages: async (sessionId) => {
    if (!isUuid(sessionId)) return;
    if (shouldSkipFetchMessages(sessionId, get())) {
      return;
    }

    const seq = ++messagesFetchSeq;
    set({ messagesLoadingSessionId: sessionId });
    try {
      const res = await apiClient.get<PaginatedResponse<MessageResponse>>(
        `/chat/sessions/${sessionId}/messages`,
        { page: 1, page_size: CHAT_MESSAGES_PAGE_SIZE, order: 'desc' },
        { headers: getChatProjectHeaders(projectIdForSession(get, sessionId)) },
      );
      if (seq !== messagesFetchSeq) return;

      const chronologicalRows = [...res.items].reverse();
      const normalized = normalizeMessageList(chronologicalRows);
      const enriched = await enrichMessagesWithSessionAttachments(sessionId, normalized);
      if (seq !== messagesFetchSeq) return;

      const dedupedEnriched = ensureChronologicalMessages(
        dedupeMessagesByIdPreserveOrder(enriched),
      );

      let appliedFullMerge = false;
      set((state) => {
        if (shouldSkipFetchMessages(sessionId, state)) {
          return {
            messagesLoadingSessionId:
              state.messagesLoadingSessionId === sessionId
                ? null
                : state.messagesLoadingSessionId,
          };
        }
        appliedFullMerge = true;
        const msgIds = new Set(dedupedEnriched.map((m) => m.id));
        const sess = state.sessions.find((s) => s.id === sessionId);
        let sessions = state.sessions;
        const pinList = sess?.pinnedMessageIds ?? [];
        if (pinList.length) {
          const nextPins = pinList.filter((id) => msgIds.has(id));
          if (nextPins.length !== pinList.length) {
            sessions = state.sessions.map((s) =>
              s.id === sessionId ? { ...s, pinnedMessageIds: nextPins } : s,
            );
            void apiClient
              .patch(`/chat/sessions/${sessionId}`, {
                metadata_patch: { pinned_message_ids: nextPins },
              }, {
                headers: getChatProjectHeaders(projectIdForSession(get, sessionId)),
              })
              .catch(() => {});
          }
        }
        return {
          messages: {
            ...state.messages,
            [sessionId]: dedupedEnriched,
          },
          messagePages: {
            ...state.messagePages,
            [sessionId]: {
              hasOlder: res.has_next,
              nextOlderPage: 2,
              isLoadingOlder: false,
            },
          },
          messagesLoadingSessionId:
            state.messagesLoadingSessionId === sessionId
              ? null
              : state.messagesLoadingSessionId,
          sessions,
        };
      });
      if (appliedFullMerge) {
        hydrateGenUiFromMessages(sessionId, dedupedEnriched);
        void hydrateSessionCanvasArtifacts(sessionId);
      }
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) {
        get().dropStaleSession(sessionId);
      }
      set((state) => ({
        messagesLoadingSessionId:
          state.messagesLoadingSessionId === sessionId
            ? null
            : state.messagesLoadingSessionId,
      }));
    }
  },

  fetchOlderMessages: async (sessionId) => {
    const pageState = get().messagePages[sessionId];
    if (!pageState?.hasOlder || pageState.isLoadingOlder) return;

    set((state) => ({
      messagePages: {
        ...state.messagePages,
        [sessionId]: { ...pageState, isLoadingOlder: true },
      },
    }));

    try {
      const res = await apiClient.get<PaginatedResponse<MessageResponse>>(
        `/chat/sessions/${sessionId}/messages`,
        {
          page: pageState.nextOlderPage,
          page_size: CHAT_MESSAGES_PAGE_SIZE,
          order: 'desc',
        },
        { headers: getChatProjectHeaders(projectIdForSession(get, sessionId)) },
      );
      const olderMessages = normalizeMessageList([...res.items].reverse());
      const existingSnapshot = get().messages[sessionId] ?? [];
      const existingIds = new Set(existingSnapshot.map((m) => m.id));
      const dedupedOlder = olderMessages.filter((m) => !existingIds.has(m.id));
      const mergedForEnrich = [...dedupedOlder, ...existingSnapshot];
      const enrichedMerged = await enrichMessagesWithSessionAttachments(
        sessionId,
        mergedForEnrich,
      );
      const dedupedMerged = ensureChronologicalMessages(
        dedupeMessagesByIdPreserveOrder(enrichedMerged),
      );
      set((state) => ({
        messages: {
          ...state.messages,
          [sessionId]: dedupedMerged,
        },
        messagePages: {
          ...state.messagePages,
          [sessionId]: {
            hasOlder: res.has_next,
            nextOlderPage: pageState.nextOlderPage + 1,
            isLoadingOlder: false,
          },
        },
      }));
      const merged = get().messages[sessionId] ?? [];
      hydrateGenUiFromMessages(sessionId, merged);
      void hydrateSessionCanvasArtifacts(sessionId);
    } catch {
      set((state) => ({
        messagePages: {
          ...state.messagePages,
          [sessionId]: { ...pageState, isLoadingOlder: false },
        },
      }));
    }
  },

  addMessage: (sessionId, message) => {
    set((state) => {
      const sessionMessages = state.messages[sessionId] || [];
      const updatedMessages = [...sessionMessages, message];
      return {
        messages: { ...state.messages, [sessionId]: updatedMessages },
        sessions: state.sessions.map((s) =>
          s.id === sessionId
            ? {
                ...s,
                messageCount: updatedMessages.length,
                updatedAt: new Date().toISOString(),
                preview: message.role === 'user' ? message.content.slice(0, 50) : s.preview,
              }
            : s
        ),
      };
    });
  },

  insertSteerMessage: (sessionId, message) => {
    set((state) => {
      const sessionMessages = state.messages[sessionId] || [];
      if (sessionMessages.some((m) => m.id === message.id)) {
        return state;
      }
      let insertIdx = sessionMessages.length;
      for (let i = sessionMessages.length - 1; i >= 0; i--) {
        if (sessionMessages[i]?.role === 'assistant') {
          insertIdx = i;
          break;
        }
      }
      const updatedMessages = [
        ...sessionMessages.slice(0, insertIdx),
        message,
        ...sessionMessages.slice(insertIdx),
      ];
      return {
        messages: { ...state.messages, [sessionId]: updatedMessages },
        sessions: state.sessions.map((s) =>
          s.id === sessionId
            ? {
                ...s,
                messageCount: updatedMessages.length,
                updatedAt: new Date().toISOString(),
                preview: message.content.slice(0, 50),
              }
            : s
        ),
      };
    });
  },

  updateMessage: (sessionId, messageId, updates) => {
    set((state) => ({
      messages: {
        ...state.messages,
        [sessionId]: (state.messages[sessionId] || []).map((m) =>
          m.id === messageId ? { ...m, ...updates } : m
        ),
      },
    }));
  },

  appendToMessage: (sessionId, messageId, content) => {
    set((state) => ({
      messages: {
        ...state.messages,
        [sessionId]: (state.messages[sessionId] || []).map((m) =>
          m.id === messageId ? { ...m, content: m.content + content } : m
        ),
      },
    }));
  },

  updateToolCall: (sessionId, messageId, toolCallId, updates) => {
    set((state) => ({
      messages: {
        ...state.messages,
        [sessionId]: (state.messages[sessionId] || []).map((m) => {
          if (m.id !== messageId) return m;
          return {
            ...m,
            toolCalls: m.toolCalls?.map((tc) =>
              tc.id === toolCallId ? { ...tc, ...updates } : tc
            ),
          };
        }),
      },
    }));
  },

  finalizeToolCalls: (sessionId, messageId, terminalStatus = 'success') => {
    set((state) => ({
      messages: {
        ...state.messages,
        [sessionId]: (state.messages[sessionId] || []).map((m) => {
          if (m.id !== messageId || !m.toolCalls?.length) return m;
          return {
            ...m,
            toolCalls: m.toolCalls.map((tc) =>
              tc.status === 'pending' || tc.status === 'running'
                ? { ...tc, status: terminalStatus }
                : tc
            ),
          };
        }),
      },
    }));
  },

  upsertTaskProgress: (sessionId, messageId, step) => {
    set((state) => ({
      messages: {
        ...state.messages,
        [sessionId]: (state.messages[sessionId] || []).map((m) => {
          if (m.id !== messageId) return m;
          const existing = m.taskProgress || [];
          const idx = existing.findIndex((item) => item.taskId === step.taskId);
          if (idx === -1) {
            return { ...m, taskProgress: [...existing, step] };
          }
          const prev = existing[idx];
          if (!prev) {
            return m;
          }
          const prevRank = TASK_STATUS_RANK[prev.status];
          const nextRank = TASK_STATUS_RANK[step.status];
          if (nextRank < prevRank) {
            return m;
          }
          if (
            nextRank === prevRank &&
            prev.label === step.label &&
            prev.progress === step.progress &&
            prev.order === step.order
          ) {
            return m;
          }
          const next = [...existing];
          next[idx] = {
            ...prev,
            ...step,
            order: step.order ?? prev.order,
          };
          return { ...m, taskProgress: next };
        }),
      },
    }));
  },

  finalizeTaskProgress: (sessionId, messageId, terminalStatus = 'completed') => {
    set((state) => ({
      messages: {
        ...state.messages,
        [sessionId]: (state.messages[sessionId] || []).map((m) => {
          if (m.id !== messageId || !m.taskProgress?.length) return m;
          return {
            ...m,
            taskProgress: m.taskProgress.map((step) =>
              step.status === 'pending' || step.status === 'in_progress'
                ? { ...step, status: terminalStatus }
                : step
            ),
          };
        }),
      },
    }));
  },

  updateWorkflowStepRun: (sessionId, messageId, stepId, partial) => {
    set((state) => ({
      messages: {
        ...state.messages,
        [sessionId]: (state.messages[sessionId] || []).map((m) => {
          if (m.id !== messageId || !m.workflow) return m;
          const prev = m.workflow.stepRuns[stepId] ?? { status: 'idle' as const };
          return {
            ...m,
            workflow: {
              ...m.workflow,
              stepRuns: {
                ...m.workflow.stepRuns,
                [stepId]: { ...prev, ...partial },
              },
            },
          };
        }),
      },
    }));
  },

  updateWorkflowEmbedRun: (sessionId, messageId, partial) => {
    set((state) => ({
      messages: {
        ...state.messages,
        [sessionId]: (state.messages[sessionId] || []).map((m) => {
          if (m.id !== messageId || !m.workflowEmbed) return m;
          const prev = m.workflowEmbed.run ?? { status: 'idle' as const };
          return {
            ...m,
            workflowEmbed: {
              ...m.workflowEmbed,
              run: { ...prev, ...partial },
            },
          };
        }),
      },
    }));
  },

  clearMessages: (sessionId) => {
    set((state) => ({
      messages: { ...state.messages, [sessionId]: [] },
      messagePages: {
        ...state.messagePages,
        [sessionId]: { hasOlder: false, nextOlderPage: 1, isLoadingOlder: false },
      },
      sessions: state.sessions.map((s) =>
        s.id === sessionId ? { ...s, messageCount: 0, preview: undefined } : s
      ),
      pendingUserInput:
        state.pendingUserInput?.sessionId === sessionId ? null : state.pendingUserInput,
      nestedAgentPreviewBySession: {
        ...state.nestedAgentPreviewBySession,
        [sessionId]: null,
      },
    }));
  },

  truncateAfterMessageId: (sessionId, messageId) => {
    const state = get();
    const list = state.messages[sessionId] || [];
    const idx = list.findIndex((m) => m.id === messageId);
    if (idx === -1) return false;
    const next = list.slice(0, idx + 1);
    let lastUserPreview: string | undefined;
    for (let i = next.length - 1; i >= 0; i--) {
      const m = next[i];
      if (m?.role === 'user') {
        lastUserPreview = m.content?.slice(0, 50);
        break;
      }
    }
    set((s) => ({
      messages: { ...s.messages, [sessionId]: next },
      sessions: s.sessions.map((sess) =>
        sess.id === sessionId
          ? {
              ...sess,
              messageCount: next.length,
              updatedAt: new Date().toISOString(),
              preview: lastUserPreview ?? sess.preview,
            }
          : sess
      ),
    }));
    return true;
  },

  remapStreamPersistedIds: (sessionId, mapping) => {
    const { clientUserId, serverUserId, clientAssistantId, serverAssistantId } = mapping;
    const userPair =
      serverUserId && serverUserId !== clientUserId
        ? ([clientUserId, serverUserId] as const)
        : null;
    const asstPair =
      serverAssistantId && serverAssistantId !== clientAssistantId
        ? ([clientAssistantId, serverAssistantId] as const)
        : null;
    if (!userPair && !asstPair) return;

    set((state) => {
      let list = [...(state.messages[sessionId] || [])];
      const applyPair = (oldId: string, newId: string) => {
        list = list.map((m) => (m.id === oldId ? { ...m, id: newId } : m));
      };
      if (userPair) applyPair(userPair[0], userPair[1]);
      if (asstPair) applyPair(asstPair[0], asstPair[1]);

      let sessions = state.sessions;
      const remapPins = (oldId: string, newId: string) => {
        sessions = sessions.map((s) => {
          if (s.id !== sessionId) return s;
          const pins = s.pinnedMessageIds ?? [];
          if (!pins.includes(oldId)) return s;
          return {
            ...s,
            pinnedMessageIds: pins.map((p) => (p === oldId ? newId : p)),
          };
        });
      };
      if (userPair) remapPins(userPair[0], userPair[1]);
      if (asstPair) remapPins(asstPair[0], asstPair[1]);

      let pendingUserInput = state.pendingUserInput;
      if (
        pendingUserInput?.sessionId === sessionId &&
        asstPair &&
        pendingUserInput.assistantMsgId === asstPair[0]
      ) {
        pendingUserInput = { ...pendingUserInput, assistantMsgId: asstPair[1] };
      }

      return {
        messages: { ...state.messages, [sessionId]: list },
        pendingUserInput,
        sessions,
      };
    });

    const genUi = useGenUiStore.getState();
    const artifacts = useArtifactStore.getState();
    if (userPair) {
      genUi.remapMessageId(sessionId, userPair[0], userPair[1]);
      artifacts.remapArtifactsMessageId(sessionId, userPair[0], userPair[1]);
    }
    if (asstPair) {
      genUi.remapMessageId(sessionId, asstPair[0], asstPair[1]);
      artifacts.remapArtifactsMessageId(sessionId, asstPair[0], asstPair[1]);
    }
  },

  getCurrentMessages: () => {
    const state = get();
    if (!state.currentSessionId) return [];
    return state.messages[state.currentSessionId] || [];
  },
});
