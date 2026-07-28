import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useDebounce, useDebouncedCallback, useDebouncedState } from './useDebounce';

describe('useDebounce', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('returns initial value immediately', () => {
    const { result } = renderHook(() => useDebounce('hello', 200));
    expect(result.current).toBe('hello');
  });

  it('updates after delay', () => {
    const { result, rerender } = renderHook(({ v }) => useDebounce(v, 200), { initialProps: { v: 'a' } });
    rerender({ v: 'b' });
    expect(result.current).toBe('a');
    act(() => { vi.advanceTimersByTime(200); });
    expect(result.current).toBe('b');
  });
});

describe('useDebouncedCallback', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('debounces callback calls', () => {
    const fn = vi.fn();
    const { result } = renderHook(() => useDebouncedCallback(fn, 100));
    act(() => { result.current.debouncedFn('x'); });
    expect(fn).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(100); });
    expect(fn).toHaveBeenCalledWith('x');
  });

  it('cancel prevents invocation', () => {
    const fn = vi.fn();
    const { result } = renderHook(() => useDebouncedCallback(fn, 100));
    act(() => { result.current.debouncedFn(); });
    act(() => { result.current.cancel(); });
    act(() => { vi.advanceTimersByTime(200); });
    expect(fn).not.toHaveBeenCalled();
  });
});

describe('useDebouncedState', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('returns debounced and immediate values', () => {
    const { result } = renderHook(() => useDebouncedState('init', 100));
    expect(result.current[0]).toBe('init'); // debounced
    expect(result.current[1]).toBe('init'); // immediate
    act(() => { result.current[2]('new'); });
    expect(result.current[1]).toBe('new');
    expect(result.current[0]).toBe('init');
    act(() => { vi.advanceTimersByTime(100); });
    expect(result.current[0]).toBe('new');
  });
});
