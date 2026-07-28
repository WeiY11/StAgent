import { describe, expect, it } from 'vitest';
import type { Message, TaskProgressStep } from '@/types/chat';
import {
  dedupeMessagesByIdPreserveOrder,
  mapApiTodosToSteps,
  isChatStreamBusyForSession,
  TASK_STATUS_RANK,
  CHAT_MESSAGES_PAGE_SIZE,
  applyTodoStatusUpdate,
  findLatestTodoAnchorMessage,
} from './chatHelpers';

const msg = (id: string, role = 'user'): Message =>
  ({ id, role, content: '', createdAt: '' } as unknown as Message);

describe('chatHelpers', () => {
  it('CHAT_MESSAGES_PAGE_SIZE is 80', () => {
    expect(CHAT_MESSAGES_PAGE_SIZE).toBe(80);
  });

  it('TASK_STATUS_RANK orders pending < in_progress < completed', () => {
    expect(TASK_STATUS_RANK.pending).toBeLessThan(TASK_STATUS_RANK.in_progress);
    expect(TASK_STATUS_RANK.in_progress).toBeLessThanOrEqual(TASK_STATUS_RANK.completed);
  });

  it('dedupeMessagesByIdPreserveOrder keeps first occurrence', () => {
    const result = dedupeMessagesByIdPreserveOrder([msg('a'), msg('b'), msg('a')]);
    expect(result.map((m) => m.id)).toEqual(['a', 'b']);
  });

  it('mapApiTodosToSteps returns undefined for empty', () => {
    expect(mapApiTodosToSteps(undefined)).toBeUndefined();
    expect(mapApiTodosToSteps([])).toBeUndefined();
  });

  it('mapApiTodosToSteps maps and sorts by order', () => {
    const steps = mapApiTodosToSteps([
      { id: '2', content: 'B', status: 'pending', order: 2 },
      { id: '1', content: 'A', status: 'completed', order: 1 },
    ])!;
    expect(steps[0]!.taskId).toBe('1');
    expect(steps[1]!.taskId).toBe('2');
  });

  it('isChatStreamBusyForSession', () => {
    expect(isChatStreamBusyForSession(null, { activeStreamSessionId: 's1', isLoading: true, isStreaming: true })).toBe(false);
    expect(isChatStreamBusyForSession('s1', { activeStreamSessionId: 's1', isLoading: true, isStreaming: false })).toBe(true);
    expect(isChatStreamBusyForSession('s2', { activeStreamSessionId: 's1', isLoading: true, isStreaming: true })).toBe(false);
    expect(isChatStreamBusyForSession('s1', { activeStreamSessionId: 's1', isLoading: false, isStreaming: false })).toBe(false);
  });

  it('applyTodoStatusUpdate updates target and demotes other in_progress', () => {
    const todos: TaskProgressStep[] = [
      { taskId: 'a', label: 'A', status: 'in_progress' },
      { taskId: 'b', label: 'B', status: 'pending' },
    ];
    const result = applyTodoStatusUpdate(todos, 'b', 'in_progress');
    expect(result[0]!.status).toBe('pending');
    expect(result[1]!.status).toBe('in_progress');
  });

  it('findLatestTodoAnchorMessage finds last assistant with taskProgress', () => {
    const m1 = { ...msg('1', 'assistant'), taskProgress: [{ taskId: 'x', label: 'X', status: 'pending' }] } as unknown as Message;
    const m2 = { ...msg('2', 'assistant') } as unknown as Message;
    expect(findLatestTodoAnchorMessage([m1, m2])).toBe(m1);
    expect(findLatestTodoAnchorMessage([m2])).toBeUndefined();
  });
});
