import { describe, expect, it } from 'vitest';
import { isUuid } from '../lib/utils/utils';

// Test the enablement logic of usePromptPreview without rendering (pure logic)
describe('usePromptPreview enablement logic', () => {
  it('isUuid validates session IDs correctly', () => {
    expect(isUuid('550e8400-e29b-41d4-a716-446655440000')).toBe(true);
    expect(isUuid('not-a-uuid')).toBe(false);
    expect(isUuid(null)).toBe(false);
    expect(isUuid(undefined)).toBe(false);
    expect(isUuid('')).toBe(false);
  });
});
