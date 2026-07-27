import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import { apiClient } from '@/api/client';
import { createQueryFn, createMutationFn } from './request-processor';

// Pure factory tests: mock the HTTP client, never render a React component.
vi.mock('@/api/client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

const mocked = apiClient as unknown as {
  get: Mock;
  post: Mock;
  put: Mock;
  patch: Mock;
  delete: Mock;
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('createQueryFn', () => {
  it('issues a GET with the url and params', async () => {
    mocked.get.mockResolvedValue({ ok: true });
    const fn = createQueryFn<{ ok: boolean }>('/items', { page: 1, active: true });
    const result = await fn();
    expect(apiClient.get).toHaveBeenCalledWith('/items', { page: 1, active: true });
    expect(result).toEqual({ ok: true });
  });
});

describe('createMutationFn', () => {
  it('POSTs the variables by default', async () => {
    mocked.post.mockResolvedValue({ id: 1 });
    const fn = createMutationFn<{ id: number }, { name: string }>('/items');
    const result = await fn({ name: 'a' });
    expect(apiClient.post).toHaveBeenCalledWith('/items', { name: 'a' });
    expect(result).toEqual({ id: 1 });
  });

  it('resolves a dynamic url function for PUT', async () => {
    mocked.put.mockResolvedValue({});
    const fn = createMutationFn<unknown, { id: number }>((v) => `/items/${v.id}`, 'PUT');
    await fn({ id: 7 });
    expect(apiClient.put).toHaveBeenCalledWith('/items/7', { id: 7 });
  });

  it('PATCHes the resolved url with variables', async () => {
    mocked.patch.mockResolvedValue({});
    const fn = createMutationFn<unknown, { id: number }>('/items/1', 'PATCH');
    await fn({ id: 1 });
    expect(apiClient.patch).toHaveBeenCalledWith('/items/1', { id: 1 });
  });

  it('DELETEs via the resolved url only', async () => {
    mocked.delete.mockResolvedValue({});
    const fn = createMutationFn<unknown, { id: number }>((v) => `/items/${v.id}`, 'DELETE');
    await fn({ id: 3 });
    expect(apiClient.delete).toHaveBeenCalledWith('/items/3');
  });
});
