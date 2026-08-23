import { describe, expect, it, vi, beforeEach } from 'vitest';
import { HttpError, setAuthTokens, clearAuthTokens, getAccessToken } from './client';

// Mock dependencies
vi.mock('@/lib/machineFingerprint', () => ({ getMachineFingerprint: () => 'abcdef1234567890' }));

describe('HttpError', () => {
  it('stores status and message', () => {
    const err = new HttpError('Not Found', 404);
    expect(err.status).toBe(404);
    expect(err.message).toBe('Not Found');
    expect(err.name).toBe('HttpError');
    expect(err).toBeInstanceOf(Error);
  });
});

describe('auth token management', () => {
  beforeEach(() => {
    localStorage.clear();
    clearAuthTokens();
  });

  it('setAuthTokens stores in memory and localStorage', () => {
    setAuthTokens('access123', 'refresh456');
    expect(getAccessToken()).toBe('access123');
    expect(localStorage.getItem('StAgent_access_token')).toBe('access123');
    expect(localStorage.getItem('StAgent_refresh_token')).toBe('refresh456');
  });

  it('clearAuthTokens removes all tokens', () => {
    setAuthTokens('a', 'b');
    clearAuthTokens();
    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem('StAgent_access_token')).toBeNull();
    expect(localStorage.getItem('StAgent_refresh_token')).toBeNull();
  });

  it('getAccessToken falls back to localStorage', () => {
    localStorage.setItem('StAgent_access_token', 'stored');
    // Reset in-memory cache by clearing module state
    clearAuthTokens();
    localStorage.setItem('StAgent_access_token', 'stored');
    expect(getAccessToken()).toBe('stored');
  });
});
