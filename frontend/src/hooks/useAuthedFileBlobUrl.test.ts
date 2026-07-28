import { describe, expect, it, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useAuthedFileBlobUrl } from './useAuthedFileBlobUrl';

vi.mock('@/api/client', () => ({ getAccessToken: () => null }));
vi.mock('@/lib/petAppearanceMime', () => ({ effectivePetImageMime: (m: string | null) => m }));

describe('useAuthedFileBlobUrl', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, blob: () => new Blob(['x']) }));
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:mock'), revokeObjectURL: vi.fn() });
  });

  it('returns null url when fileId is null', () => {
    const { result } = renderHook(() => useAuthedFileBlobUrl(null, 'image/png'));
    expect(result.current.url).toBeNull();
    expect(result.current.isPending).toBe(false);
  });

  it('returns null url when mime is null', () => {
    const { result } = renderHook(() => useAuthedFileBlobUrl('f1', null));
    expect(result.current.url).toBeNull();
  });
});
