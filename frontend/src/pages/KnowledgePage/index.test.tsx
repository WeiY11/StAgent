import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider } from '@/components/ui/Toaster';
import { useKnowledgeStore } from '@/stores/knowledge';
import KnowledgePage from './index';

const mocks = vi.hoisted(() => ({
  reindex: vi.fn().mockResolvedValue({
    id: 'doc-1',
    status: 'processing',
    is_indexed: false,
    task_id: 'task-1',
  }),
  document: {
    id: 'doc-1',
    name: 'broken.pdf',
    original_name: 'broken.pdf',
    file_type: 'document',
    mime_type: 'application/pdf',
    size: 2048,
    status: 'failed',
    has_ocr: false,
    is_indexed: false,
    summary: null as string | null,
    created_at: '2026-07-29T00:00:00Z',
    updated_at: '2026-07-29T00:00:00Z',
  },
}));

vi.mock('@/hooks/useRealtimeFileSync', () => ({
  useRealtimeFileSync: vi.fn(),
}));

vi.mock('@/hooks/useFolders', () => ({
  useFolderTree: () => ({ data: [] }),
  useCreateFolder: () => ({ mutateAsync: vi.fn() }),
  useDeleteFolder: () => ({ mutateAsync: vi.fn() }),
}));

vi.mock('@/hooks/useKnowledge', () => ({
  useDocuments: () => ({
    data: {
      items: [mocks.document],
      total: 1,
      page: 1,
      page_size: 20,
      has_next: false,
      has_prev: false,
    },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
  useUploadDocument: () => ({ mutateAsync: vi.fn() }),
  useReindexDocument: () => ({
    mutateAsync: mocks.reindex,
    isPending: false,
  }),
}));

describe('KnowledgePage', () => {
  beforeEach(() => {
    mocks.reindex.mockClear();
    Object.assign(mocks.document, {
      name: 'broken.pdf',
      original_name: 'broken.pdf',
      status: 'failed',
      is_indexed: false,
      summary: null,
    });
    useKnowledgeStore.getState().setSelectedDocument(null);
    useKnowledgeStore.getState().setPreviewOpen(false);
  });

  it('lets the user retry indexing a failed document from its preview', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <KnowledgePage />
        </ToastProvider>
      </QueryClientProvider>,
    );

    await user.click(screen.getByText('broken.pdf'));
    const retry = screen.getByRole('button', { name: 'Retry indexing' });
    await user.click(retry);

    expect(mocks.reindex).toHaveBeenCalledWith('doc-1');
  });

  it('lets an indexed legacy document rebuild missing summary and chunks', async () => {
    Object.assign(mocks.document, {
      name: 'legacy.pdf',
      original_name: 'legacy.pdf',
      status: 'processed',
      is_indexed: true,
      summary: null,
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <KnowledgePage />
        </ToastProvider>
      </QueryClientProvider>,
    );

    await user.click(screen.getByText('legacy.pdf'));
    const reindex = screen.getByRole('button', { name: 'Reindex' });
    await user.click(reindex);

    expect(mocks.reindex).toHaveBeenCalledWith('doc-1');
  });
});
