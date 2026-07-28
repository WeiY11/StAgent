import { describe, expect, it } from 'vitest';
import { escapeHtml } from './highlight';

describe('escapeHtml', () => {
  it('escapes all special characters', () => {
    expect(escapeHtml('<div class="a">&\'b\'</div>')).toBe(
      '&lt;div class=&quot;a&quot;&gt;&amp;&#39;b&#39;&lt;/div&gt;',
    );
  });

  it('returns empty string for empty input', () => {
    expect(escapeHtml('')).toBe('');
  });

  it('passes through plain text', () => {
    expect(escapeHtml('hello world')).toBe('hello world');
  });
});
