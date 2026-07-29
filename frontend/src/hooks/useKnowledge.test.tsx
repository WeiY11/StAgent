import type { PropsWithChildren } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/api/client';
import { shouldPollDocuments, useReindexDocument } from './useKnowledge';

vi.mock('@/api/client', () => ({
  apiClient: {
    post: vi.fn(),
  },
}));

vi.mock('./useRealtimeFileSync', () => ({
  emitRealtimeFileEvent: vi.fn(),
}));

describe('useReindexDocument', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('requeues a document and exposes the accepted processing state', async () => {
    const response = {
      id: 'doc-1',
      status: 'processing',
      is_indexed: false,
      task_id: 'task-1',
    };
    vi.mocked(apiClient.post).mockResolvedValue(response);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const wrapper = ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useReindexDocument(), { wrapper });

    let accepted:
      | Awaited<ReturnType<typeof result.current.mutateAsync>>
      | undefined;
    await act(async () => {
      accepted = await result.current.mutateAsync('doc-1');
    });

    expect(accepted).toEqual(response);
    expect(apiClient.post).toHaveBeenCalledWith('/documents/doc-1/reindex');
  });
});

describe('shouldPollDocuments', () => {
  it('polls only while a document is waiting for its real index result', () => {
    expect(shouldPollDocuments([])).toBe(false);
    expect(
      shouldPollDocuments([{ status: 'uploaded', is_indexed: false }]),
    ).toBe(true);
    expect(
      shouldPollDocuments([{ status: 'processing', is_indexed: false }]),
    ).toBe(true);
    expect(
      shouldPollDocuments([{ status: 'uploaded', is_indexed: true }]),
    ).toBe(false);
    expect(
      shouldPollDocuments([{ status: 'failed', is_indexed: false }]),
    ).toBe(false);
  });
});
