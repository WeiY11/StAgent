import { useMemo } from 'react';
import { isAdminUser, userHasAnyPermission, userHasPermissions } from '@/lib/auth/authUser';
import { useAuthStore } from '@/stores/auth';

export function usePermissions() {
  const user = useAuthStore((state) => state.user);

  return useMemo(
    () => ({
      hasPermission: (...keys: string[]) => userHasPermissions(user, ...keys),
      can: (...keys: string[]) => userHasPermissions(user, ...keys),
      canAny: (...keys: string[]) => userHasAnyPermission(user, ...keys),
      hasRole: (...roles: string[]) => {
        if (!user) return false;
        if (isAdminUser(user)) return true;
        return roles.some((role) => user.roles.includes(role));
      },
      isSuperuser: Boolean(user?.is_superuser),
      permissions: user?.permissions ?? [],
      roles: user?.roles ?? [],
      user,
    }),
    [user],
  );
}

export default usePermissions;
