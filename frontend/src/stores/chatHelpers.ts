/**
 * Pure types, constants, and helper functions for the chat store.
 * Extracted to reduce the main chat.ts file size and improve readability.
 */
import i18n from '@/i18n';
import type { ChatSession, Message, TaskProgressStep } from '@/types/chat';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const CHAT_MESSAGES_PAGE_SIZE = 80;

export const TASK_STATUS_RANK: Record<TaskProgressStep['status'], number> = {
  pending: 0,
  in_progress: 1,
  completed: 2,
  failed: 2,
  cancelled: 2,
};

export const TODO_WRITE_TOOL_NAMES = new Set([
  'todo_write',
  'todo',
  'todo_create',
  'update_todo',
  'create_todo',
]);

// ---------------------------------------------------------------------------
// Interfaces
// ---------------------------------------------------------------------------

export interface SessionResponse {
  id: string;
  name: string;
  project_id?: string | null;
  message_count: number;
  created_at: string;
  updated_at: string;
  pinned_message_ids?: string[];
  todos?: Array<{
    id: string;
    content: string;
    status: TaskProgressStep['status'];
    order?: number;
  }>;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
  has_prev: boolean;
}

export interface MessagePageState {
  hasOlder: boolean;
  nextOlderPage: number;
  isLoadingOlder: boolean;
}

export interface SessionTodoUiState {
  pinned: boolean;
  dismissed: boolean;
}

// ---------------------------------------------------------------------------
// Pure helper functions
// ---------------------------------------------------------------------------

/** Keeps first occurrence of each message id (chronological merge order preserved). */
export function dedupeMessagesByIdPreserveOrder(messages: Message[]): Message[] {
  const seen = new Set<string>();
  const out: Message[] = [];
  for (const m of messages) {
    if (seen.has(m.id)) continue;
    seen.add(m.id);
    out.push(m);
  }
  return out;
}

export function mapApiTodosToSteps(
  todos: SessionResponse['todos'],
): TaskProgressStep[] | undefined {
  if (!Array.isArray(todos) || todos.length === 0) return undefined;
  return todos
    .map((item, index) => ({
      taskId: String(item.id),
      label: String(item.content || item.id),
      status: item.status,
      order: item.order ?? index,
    }))
    .sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
}

export function findLatestTodoAnchorMessage(messages: Message[]): Message | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m || m.role !== 'assistant') continue;
    if (m.taskProgress?.length) return m;
    if (m.toolCalls?.some((tc) => TODO_WRITE_TOOL_NAMES.has(tc.name))) return m;
  }
  return undefined;
}

export function applyTodoStatusUpdate(
  todos: TaskProgressStep[],
  taskId: string,
  status: TaskProgressStep['status'],
): TaskProgressStep[] {
  return todos.map((item) => {
    if (item.taskId === taskId) {
      return { ...item, status };
    }
    if (status === 'in_progress' && item.status === 'in_progress') {
      return { ...item, status: 'pending' as const };
    }
    return item;
  });
}

export function mapSession(s: SessionResponse): ChatSession {
  const pins = s.pinned_message_ids;
  const base: ChatSession = {
    id: s.id,
    title: s.name || i18n.t('chat.defaultSessionName'),
    createdAt: s.created_at,
    updatedAt: s.updated_at,
    messageCount: s.message_count,
    projectId: s.project_id ?? null,
    pinnedMessageIds: Array.isArray(pins) ? pins.map(String) : [],
  };
  const todos = mapApiTodosToSteps(s.todos);
  if (todos?.length) {
    base.todos = todos;
  }
  return base;
}

/** True when this thread owns the in-flight chat stream (composer should block send). */
export function isChatStreamBusyForSession(
  sessionId: string | null | undefined,
  state: {
    activeStreamSessionId: string | null;
    isLoading: boolean;
    isStreaming: boolean;
  },
): boolean {
  if (!sessionId) return false;
  return (
    state.activeStreamSessionId === sessionId &&
    (state.isLoading || state.isStreaming)
  );
}
