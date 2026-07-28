import { describe, expect, it } from 'vitest';
import { parseCsvForPreview } from './parseCsvForPreview';

describe('parseCsvForPreview', () => {
  it('parses simple CSV', () => {
    const r = parseCsvForPreview('a,b\n1,2');
    expect(r.rows).toEqual([['a', 'b'], ['1', '2']]);
    expect(r.truncatedRows).toBe(false);
  });

  it('handles quoted fields with commas', () => {
    const r = parseCsvForPreview('"a,b",c');
    expect(r.rows).toEqual([['a,b', 'c']]);
  });

  it('handles escaped quotes', () => {
    const r = parseCsvForPreview('"a""b"');
    expect(r.rows).toEqual([['a"b']]);
  });

  it('truncates rows beyond MAX_ROWS', () => {
    const input = Array.from({ length: 600 }, (_, i) => `row${i}`).join('\n');
    const r = parseCsvForPreview(input);
    expect(r.truncatedRows).toBe(true);
  });

  it('handles empty input', () => {
    const r = parseCsvForPreview('');
    expect(r.rows).toEqual([['']]);
  });

  it('handles CRLF line endings', () => {
    const r = parseCsvForPreview('a,b\r\n1,2\r\n');
    expect(r.rows).toEqual([['a', 'b'], ['1', '2']]);
  });
});
