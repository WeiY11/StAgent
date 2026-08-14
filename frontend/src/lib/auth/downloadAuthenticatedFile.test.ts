import { beforeEach, describe, expect, it, vi } from 'vitest';
import { openAuthenticatedFilePreview } from './downloadAuthenticatedFile';

vi.mock('@/api/client', () => ({
  getAccessToken: vi.fn(() => 'access-token'),
}));

describe('openAuthenticatedFilePreview', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('<script>window.pwned = true</script>', {
        headers: {
          'Content-Disposition': 'attachment; filename="danger.html"',
          'Content-Type': 'application/octet-stream',
        },
      })),
    );
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:preview-test'),
    });
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: vi.fn(),
    });
  });

  it('downloads an unsafe preview response instead of opening its blob URL', async () => {
    const opened = vi.spyOn(window, 'open');
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, 'click');

    await openAuthenticatedFilePreview('file-1');

    expect(opened).not.toHaveBeenCalled();
    expect(clicked).toHaveBeenCalledOnce();
  });
});
