import type { KnowledgeDocument } from '@/stores/knowledge';
import type { FolderTreeNode } from '@/hooks/useFolders';

export interface DocumentSearchResult {
  id: string;
  name: string;
  file_type: string;
  score: number;
  snippet?: string | null;
  chunk_id?: string | null;
  start_offset?: number | null;
  end_offset?: number | null;
}

export interface DocumentSearchResponse {
  query: string;
  results: DocumentSearchResult[];
  total: number;
}

export type IndexStatusKey = 'indexed' | 'processing' | 'failed' | 'pending';

export function deriveIndexStatus(
  status?: string,
  isIndexed?: boolean,
): IndexStatusKey {
  const normalized = (status ?? '').toLowerCase();
  if (normalized === 'failed') return 'failed';
  if (normalized === 'processing' || normalized === 'uploaded') {
    return isIndexed ? 'indexed' : 'processing';
  }
  if (isIndexed) return 'indexed';
  if (normalized === 'processed' && !isIndexed) return 'pending';
  return isIndexed ? 'indexed' : 'pending';
}

export function indexStatusBadgeVariant(
  key: IndexStatusKey,
): 'success' | 'warning' | 'error' | 'default' {
  switch (key) {
    case 'indexed':
      return 'success';
    case 'processing':
      return 'warning';
    case 'failed':
      return 'error';
    default:
      return 'default';
  }
}

export function mapSearchResultToDocument(r: DocumentSearchResult): KnowledgeDocument {
  const ft = r.file_type;
  const typeStr =
    ft === 'image'
      ? 'image/png'
      : ft === 'document'
        ? 'application/pdf'
        : ft === 'data'
          ? 'application/vnd.ms-excel'
          : 'application/octet-stream';
  return {
    id: String(r.id),
    name: r.name,
    type: typeStr,
    size: 0,
    url: `/api/v1/files/${r.id}`,
    preview: r.snippet ?? undefined,
    summary: r.snippet ?? null,
    isIndexed: true,
    status: 'processed',
    createdAt: new Date().toISOString(),
  };
}

/** Drop coding-project folders from the knowledge tree. */
export function filterProjectNodes(nodes: FolderTreeNode[]): FolderTreeNode[] {
  return nodes
    .filter((n) => !n.is_project)
    .map((n) => ({
      ...n,
      children: filterProjectNodes(n.children ?? []),
    }));
}

export function findFolderPath(
  nodes: FolderTreeNode[],
  targetId: string,
): FolderTreeNode[] | null {
  for (const node of nodes) {
    if (node.is_project) continue;
    if (node.id === targetId) return [node];
    if (node.children?.length) {
      const childPath = findFolderPath(node.children, targetId);
      if (childPath) return [node, ...childPath];
    }
  }
  return null;
}
