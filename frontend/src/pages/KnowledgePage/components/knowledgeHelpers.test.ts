import { describe, expect, it } from 'vitest';
import {
  deriveIndexStatus,
  mapSearchResultToDocument,
} from './knowledgeHelpers';

describe('knowledgeHelpers', () => {
  it('does not report an uploaded document as indexed before chunks exist', () => {
    expect(deriveIndexStatus('uploaded', false)).toBe('processing');
    expect(deriveIndexStatus('uploaded', true)).toBe('indexed');
  });

  it('preserves catalog metadata when mapping a content-search result', () => {
    const document = mapSearchResultToDocument({
      id: 'doc-1',
      name: 'paper.pdf',
      file_type: 'document',
      mime_type: 'application/pdf',
      size: 4096,
      status: 'processed',
      is_indexed: true,
      summary: 'Catalog summary',
      created_at: '2026-07-29T00:00:00Z',
      score: 1.5,
      snippet: 'matching passage',
    });

    expect(document).toMatchObject({
      id: 'doc-1',
      name: 'paper.pdf',
      type: 'application/pdf',
      size: 4096,
      status: 'processed',
      isIndexed: true,
      summary: 'Catalog summary',
      preview: 'matching passage',
      createdAt: '2026-07-29T00:00:00Z',
    });
  });
});
