import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '@/api/client';
import { emitRealtimeFileEvent } from './useRealtimeFileSync';

export interface DocumentFile {
  id: string;
  name: string;
  original_name: string;
  file_type: string;
  mime_type?: string;
  size: number;
  status: string;
  user_id?: string;
  folder_id?: string;
  space_id?: string;
  checksum?: string;
  page_count?: number;
  has_ocr: boolean;
  is_indexed: boolean;
  summary?: string | null;
  created_at: string;
  updated_at: string;
  workspace_id?: string | null;
  knowledge_space_id?: string | null;
  access_scope?: 'private' | 'space' | 'restricted';
}

export interface KnowledgeSpace {
  id: string;
  workspace_id: string;
  name: string;
  slug: string;
  description?: string | null;
  default_access_scope: 'private' | 'space' | 'restricted';
  is_default: boolean;
}

export function useKnowledgeSpaces() {
  return useQuery({
    queryKey: ['knowledge', 'spaces'],
    queryFn: () => apiClient.get<KnowledgeSpace[]>('/knowledge/spaces'),
    staleTime: 60_000,
  });
}

export function useCreateKnowledgeSpace() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: { name: string; slug?: string; description?: string }) =>
      apiClient.post<KnowledgeSpace>('/knowledge/spaces', payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge', 'spaces'] });
    },
  });
}

interface PaginatedDocuments {
  items: DocumentFile[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
  has_prev: boolean;
}

export function shouldPollDocuments(
  documents: Array<Pick<DocumentFile, 'status' | 'is_indexed'>>,
): boolean {
  return documents.some((document) => {
    const status = document.status.toLowerCase();
    return (
      !document.is_indexed &&
      (status === 'uploaded' || status === 'processing')
    );
  });
}

export function useDocuments(params: {
  search?: string;
  file_type?: string;
  folder_id?: string;
  space_id?: string;
  /** When true and folder_id is omitted, only docs with no folder. */
  unfiled?: boolean;
  page?: number;
  page_size?: number;
  enabled?: boolean;
  scopeKey?: string | null;
}) {
  return useQuery({
    queryKey: [
      'documents',
      'list',
      {
        search: params.search,
        file_type: params.file_type,
        folder_id: params.folder_id,
        space_id: params.space_id,
        unfiled: params.unfiled ?? false,
        page: params.page,
        page_size: params.page_size,
        scopeKey: params.scopeKey ?? 'global',
      },
    ],
    queryFn: async () => {
      const res = await apiClient.get<PaginatedDocuments>('/documents', {
        page: params.page ?? 1,
        page_size: params.page_size ?? 50,
        ...(params.search ? { search: params.search } : {}),
        ...(params.file_type ? { file_type: params.file_type } : {}),
        ...(params.folder_id ? { folder_id: params.folder_id } : {}),
        ...(params.space_id ? { space_id: params.space_id } : {}),
        ...(params.unfiled && !params.folder_id ? { unfiled: true } : {}),
      } as Record<string, string | number | boolean | undefined>);
      return {
        items: res.items,
        total: res.total,
        page: res.page,
        page_size: res.page_size,
        has_next: res.has_next,
        has_prev: res.has_prev,
      };
    },
    enabled: params.enabled !== false,
    refetchInterval: (query) =>
      shouldPollDocuments(query.state.data?.items ?? []) ? 2_500 : false,
  });
}

export function useUploadDocument() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      file,
      folder_id,
      space_id,
      access_scope,
    }: {
      file: File;
      folder_id?: string;
      space_id?: string;
      access_scope?: 'private' | 'space' | 'restricted';
    }) => {
      const formData = new FormData();
      formData.append('file', file);
      if (folder_id) formData.append('folder_id', folder_id);
      if (space_id) formData.append('space_id', space_id);
      if (access_scope) formData.append('access_scope', access_scope);
      return apiClient.upload('/documents/upload', formData);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents'] });
      queryClient.invalidateQueries({ queryKey: ['folders'] });
      emitRealtimeFileEvent('uploaded');
    },
  });
}

export interface ReindexDocumentResponse {
  id: string;
  status: string;
  is_indexed: boolean;
  task_id?: string | null;
}

export function useReindexDocument() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (documentId: string) =>
      apiClient.post<ReindexDocumentResponse>(`/documents/${documentId}/reindex`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents'] });
      emitRealtimeFileEvent('updated');
    },
  });
}

export type PromoteToKnowledgeResponse = {
  promoted: Array<{
    id: string;
    name: string;
    original_name: string;
    file_type: string;
    mime_type?: string;
    size: number;
    status: string;
  }>;
  skipped: Array<{ id: string; reason: string }>;
};

export function usePromoteToKnowledge() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (payload: { file_ids: string[]; session_id?: string }) =>
      apiClient.post<PromoteToKnowledgeResponse>('/documents/promote', payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents'] });
      emitRealtimeFileEvent('uploaded');
    },
  });
}

export function useSearchDocuments() {
  return useMutation({
    mutationFn: async ({
      query,
      file_types,
      folder_id,
      limit = 10,
    }: {
      query: string;
      file_types?: string;
      folder_id?: string;
      limit?: number;
    }) => {
      return apiClient.get('/documents/search', {
        query,
        file_types,
        folder_id,
        limit,
      } as Record<string, string | number | boolean | undefined>);
    },
  });
}
