import { describe, expect, it } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useProductMeta } from './useProductMeta';

describe('useProductMeta', () => {
  it('throws when used outside provider', () => {
    // Suppress console.error from React error boundary
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => renderHook(() => useProductMeta())).toThrow(
      'useProductMeta must be used within <ProductMetaProvider>',
    );
    spy.mockRestore();
  });
});
