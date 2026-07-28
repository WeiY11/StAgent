import { describe, expect, it, beforeEach } from 'vitest';
import { getCachedPetBubbleGreeting } from './useDailyChatGreetings';

const LS_KEY = 'leagent.chatDailyPetBubbles.v1';

describe('getCachedPetBubbleGreeting', () => {
  beforeEach(() => localStorage.clear());

  it('returns null when no cache', () => {
    expect(getCachedPetBubbleGreeting()).toBeNull();
  });

  it('returns a random line from today cache', () => {
    const today = new Date().toISOString().slice(0, 10);
    localStorage.setItem(LS_KEY, JSON.stringify({ date: today, locale: 'en', lines: ['Hi', 'Hello'] }));
    const result = getCachedPetBubbleGreeting();
    expect(['Hi', 'Hello']).toContain(result);
  });

  it('returns null for stale date', () => {
    localStorage.setItem(LS_KEY, JSON.stringify({ date: '2020-01-01', locale: 'en', lines: ['old'] }));
    expect(getCachedPetBubbleGreeting()).toBeNull();
  });
});
