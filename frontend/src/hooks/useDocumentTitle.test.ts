import { describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useDocumentTitle } from './useDocumentTitle';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => `translated:${k}`, i18n: { language: 'en' } }),
}));

describe('useDocumentTitle', () => {
  it('sets document.title from i18n key', () => {
    renderHook(() => useDocumentTitle('page.home'));
    expect(document.title).toBe('translated:page.home');
  });
});
