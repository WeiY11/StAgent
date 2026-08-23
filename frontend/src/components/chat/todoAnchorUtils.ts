import type { Message } from '@/types/chat';

const TODO_WRITE_TOOL_NAMES = new Set([
  'todo_write',
  'todo',
  'todo_create',
  'update_todo',
  'create_todo',
]);

export function isTodoWriteToolName(name: string | undefined): boolean {
  if (!name) return false;
  return TODO_WRITE_TOOL_NAMES.has(name);
}

/** Whether this assistant message invoked or snapshot todo_write activity. */
export function messageHasTodoActivity(message: Message): boolean {
  if (message.role !== 'assistant') return false;
  if (message.taskProgress?.length) return true;
  return message.toolCalls?.some((tc) => isTodoWriteToolName(tc.name)) ?? false;
}

/**
 * Latest assistant message in the thread that owns the inline todo list.
 *
 * Memoised on the array reference: the Zustand selector in AgentMessage runs
 * per store tick for every mounted row (~60×/s while streaming), and all rows
 * share the same messages array per flush, so only the first call scans.
 */
let _lastAnchorInput: Message[] | null = null;
let _lastAnchorResult: string | null = null;

export function findLatestTodoAnchorMessageId(messages: Message[]): string | null {
  if (messages === _lastAnchorInput) return _lastAnchorResult;
  let result: string | null = null;
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m || m.role !== 'assistant') continue;
    if (messageHasTodoActivity(m)) {
      result = m.id;
      break;
    }
  }
  _lastAnchorInput = messages;
  _lastAnchorResult = result;
  return result;
}
