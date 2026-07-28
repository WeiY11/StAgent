/**
 * Shared types for chat store slices.
 *
 * The full `ChatStore` type is the intersection of every slice's state + actions.
 * Each slice file imports `ChatStore` from here so its `StateCreator` callbacks
 * can call cross-slice actions (e.g. a session action calling `fetchMessages`).
 */

import type {
  ChatSession,
  ChatWorkflowEmbedRunRecord,
  ChatWorkflowStepRunRecord,
  Message,
  NestedAgentPreviewState,
  PendingUserInput,
  TaskProgressStep,
  ToolCall,
} from '@/types/chat';

import type {
  MessagePageState,
  SessionTodoUiState,
} from '../chatHelpers';

// ---------------------------------------------------------------------------
// Session slice
// ---------------------------------------------------------------------------

export interface SessionSliceState {
  sessions: ChatSession[];
  currentSessionId: string | null;
  synced: boolean;
  /** True after the first ``fetchSessions`` attempt (success or failure). */
  chatSessionsReconciled: boolean;
}

export interface SessionSliceActions {
  fetchSessions: (projectId?: string | null) => Promise<void>;
  createSession: (title?: string, projectId?: string | null) => Promise<string>;
  deleteSession: (id: string) => Promise<void>;
  /** Server returned 404 — remove thread from local state without calling DELETE. */
  dropStaleSession: (id: string) => void;
  selectSession: (id: string) => void;
  updateSessionTitle: (id: string, title: string) => void;
  /** GET /chat/sessions/:id — refresh pins (and title/count) for one session. */
  fetchSessionDetail: (sessionId: string) => Promise<void>;
  getCurrentSession: () => ChatSession | null;
}

// ---------------------------------------------------------------------------
// Messages slice
// ---------------------------------------------------------------------------

export interface MessagesSliceState {
  messages: Record<string, Message[]>;
  messagePages: Record<string, MessagePageState>;
  /** When set, `fetchMessages` is in flight for this session. */
  messagesLoadingSessionId: string | null;
}

export interface MessagesSliceActions {
  fetchMessages: (sessionId: string) => Promise<void>;
  fetchOlderMessages: (sessionId: string) => Promise<void>;
  addMessage: (sessionId: string, message: Message) => void;
  /** Insert a mid-turn steer user row before the in-flight assistant message. */
  insertSteerMessage: (sessionId: string, message: Message) => void;
  updateMessage: (sessionId: string, messageId: string, updates: Partial<Message>) => void;
  appendToMessage: (sessionId: string, messageId: string, content: string) => void;
  updateToolCall: (sessionId: string, messageId: string, toolCallId: string, updates: Partial<ToolCall>) => void;
  finalizeToolCalls: (
    sessionId: string,
    messageId: string,
    terminalStatus?: Extract<ToolCall['status'], 'success' | 'error'>,
  ) => void;
  upsertTaskProgress: (sessionId: string, messageId: string, step: TaskProgressStep) => void;
  finalizeTaskProgress: (
    sessionId: string,
    messageId: string,
    terminalStatus?: TaskProgressStep['status'],
  ) => void;
  updateWorkflowStepRun: (
    sessionId: string,
    messageId: string,
    stepId: string,
    partial: Partial<ChatWorkflowStepRunRecord>,
  ) => void;
  updateWorkflowEmbedRun: (
    sessionId: string,
    messageId: string,
    partial: Partial<ChatWorkflowEmbedRunRecord>,
  ) => void;
  clearMessages: (sessionId: string) => void;
  /** Keep messages up to and including `messageId`; drop the rest. Returns false if id not found. */
  truncateAfterMessageId: (sessionId: string, messageId: string) => boolean;
  /** Map optimistic client message ids to DB UUIDs after `/chat/stream` persist. */
  remapStreamPersistedIds: (
    sessionId: string,
    mapping: {
      clientUserId: string;
      serverUserId?: string;
      clientAssistantId: string;
      serverAssistantId?: string;
    },
  ) => void;
  getCurrentMessages: () => Message[];
}

// ---------------------------------------------------------------------------
// Streaming slice
// ---------------------------------------------------------------------------

export interface StreamingSliceState {
  /** Which session owns the in-flight `/chat/stream`. */
  activeStreamSessionId: string | null;
  isLoading: boolean;
  isStreaming: boolean;
  error: string | null;
  /** Active `/chat/stream` fetch — survives ChatView unmount. */
  streamAbortController: AbortController | null;
  /** Whether the last stream was stopped by the user (supports "continue" flow). */
  lastStopWasUserInitiated: boolean;
  /** Terminal reason from the last agent turn. */
  lastTerminalReason: string | null;
  /** Checkpoint id from the last agent turn (enables durable resume). */
  lastCheckpointId: string | null;
  /** Active ``ask_user`` questionnaire for the current thread. */
  pendingUserInput: PendingUserInput | null;
}

export interface StreamingSliceActions {
  setLoading: (loading: boolean) => void;
  setStreaming: (streaming: boolean) => void;
  /** Claim this session as the active stream owner. */
  beginChatStreamSession: (sessionId: string) => void;
  /** Clear loading/streaming only if this session still owns the stream. */
  releaseChatStreamSession: (sessionId: string) => void;
  /** Release stream ownership and reload canonical message order from the server. */
  releaseChatStreamSessionAndResync: (sessionId: string) => void;
  /** Abort the current fetch if it belongs to another session. */
  abortActiveStreamUnlessSession: (sessionId: string) => void;
  setError: (error: string | null) => void;
  setPendingUserInput: (payload: PendingUserInput | null) => void;
  clearPendingUserInput: () => void;
  setStreamAbortController: (controller: AbortController | null) => void;
  /** Clear ref only if it still points to this instance. */
  releaseStreamAbortController: (instance: AbortController) => void;
  /** User Stop button or logout — aborts in-flight stream. */
  abortChatStreamFromUser: () => void;
  /** Cancel backend agent session — kills running tasks and subprocesses. */
  cancelBackendSession: (sessionId: string) => Promise<void>;
}

// ---------------------------------------------------------------------------
// Features slice (todos, pins, coding projects, nested agent preview)
// ---------------------------------------------------------------------------

export interface FeaturesSliceState {
  /** Sub-agent tool-arg stream — keyed by session id; live-only. */
  nestedAgentPreviewBySession: Record<string, NestedAgentPreviewState | null>;
  /** Per-session todo panel: pinned to chat top + user dismissed state. */
  sessionTodoUi: Record<string, SessionTodoUiState>;
  /**
   * Ephemeral: coding projects started via ``coding_project_run`` in a thread
   * (not persisted) — used to stop dev servers when leaving the conversation.
   */
  codingProjectIdsBySession: Record<string, string[]>;
}

export interface FeaturesSliceActions {
  setNestedAgentPreview: (sessionId: string, preview: NestedAgentPreviewState | null) => void;
  setSessionTodos: (sessionId: string, todos: TaskProgressStep[]) => void;
  upsertSessionTodoFromProgress: (sessionId: string, step: TaskProgressStep) => void;
  /** Persist a manual todo status change (Cursor-style click cycle). */
  patchSessionTodoStatus: (
    sessionId: string,
    taskId: string,
    status: TaskProgressStep['status'],
  ) => Promise<void>;
  setSessionTodoPinned: (sessionId: string, pinned: boolean) => void;
  dismissSessionTodoPanel: (sessionId: string) => void;
  /** Replace ordered pin list; persists via metadata_patch. */
  setPinnedMessageIds: (sessionId: string, messageIds: string[]) => Promise<void>;
  /** Toggle one message id in the pin list (optimistic + PATCH). */
  togglePinMessage: (sessionId: string, messageId: string) => Promise<void>;
  registerCodingProjectForSession: (sessionId: string, projectId: string) => void;
  stopCodingProjectsForSession: (sessionId: string | null | undefined) => Promise<void>;
}

// ---------------------------------------------------------------------------
// Composed ChatStore
// ---------------------------------------------------------------------------

export type ChatStore =
  SessionSliceState &
  SessionSliceActions &
  MessagesSliceState &
  MessagesSliceActions &
  StreamingSliceState &
  StreamingSliceActions &
  FeaturesSliceState &
  FeaturesSliceActions;
