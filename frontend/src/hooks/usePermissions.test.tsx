import { renderHook, act } from '@testing-library/react';
import { describe, expect, afterEach, it } from 'vitest';
import type { AuthUser } from '@/lib/authUser';
import { usePermissions } from '@/hooks/usePermissions';
import { useAuthStore } from '@/stores/auth';

function setUser(user: AuthUser | null) {
  act(() => {
    useAuthStore.setState({
      user,
      isAuthenticated: Boolean(user),
    });
  });
}

function user(overrides: Partial<AuthUser> = {}): AuthUser {
  return {
    id: 'u1',
    username: 'user',
    email: 'user@localhost',
    role: 'user',
    is_superuser: false,
    permissions: [],
    roles: ['user'],
    default_workspace_id: null,
    ...overrides,
  };
}

describe('usePermissions', () => {
  afterEach(() => {
    setUser(null);
  });

  it('does not grant permissions without a user', () => {
    setUser(null);
    const { result } = renderHook(() => usePermissions());

    expect(result.current.can('workflow:admin')).toBe(false);
    expect(result.current.canAny('workflow:run')).toBe(false);
    expect(result.current.hasRole('admin')).toBe(false);
    expect(result.current.isSuperuser).toBe(false);
  });

  it('uses real permissions and roles from the auth store', () => {
    setUser(user({ permissions: ['workflow:run'], roles: ['user', 'operator'] }));
    const { result } = renderHook(() => usePermissions());

    expect(result.current.can('workflow:run')).toBe(true);
    expect(result.current.can('workflow:admin')).toBe(false);
    expect(result.current.canAny('workflow:admin', 'workflow:run')).toBe(true);
    expect(result.current.hasRole('operator')).toBe(true);
    expect(result.current.hasRole('admin')).toBe(false);
  });

  it('treats admin users as wildcard principals', () => {
    setUser(
      user({
        role: 'admin',
        is_superuser: true,
        permissions: ['*'],
        roles: ['admin'],
      }),
    );
    const { result } = renderHook(() => usePermissions());

    expect(result.current.can('workflow:admin')).toBe(true);
    expect(result.current.canAny('missing')).toBe(true);
    expect(result.current.hasRole('anything')).toBe(true);
    expect(result.current.isSuperuser).toBe(true);
  });
});
