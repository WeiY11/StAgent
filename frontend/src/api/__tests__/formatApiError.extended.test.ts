import { describe, expect, it } from 'vitest';
import { formatHttpErrorDetail } from '../formatApiError';

describe('formatHttpErrorDetail (extended)', () => {
  it('returns string detail as-is', () => {
    expect(formatHttpErrorDetail('Bad request', 400)).toBe('Bad request');
  });

  it('joins array of {msg} objects', () => {
    expect(formatHttpErrorDetail([{ msg: 'field required' }, { msg: 'too short' }], 422))
      .toBe('field required; too short');
  });

  it('handles array of plain values', () => {
    expect(formatHttpErrorDetail(['err1', 'err2'], 400)).toBe('err1; err2');
  });

  it('uses fallbackMessage when no detail', () => {
    expect(formatHttpErrorDetail(undefined, 500, 'Server error')).toBe('Server error');
  });

  it('falls back to HTTP status', () => {
    expect(formatHttpErrorDetail(undefined, 503)).toBe('HTTP 503');
  });

  it('ignores empty string detail', () => {
    expect(formatHttpErrorDetail('  ', 400, 'fallback')).toBe('fallback');
  });
});
