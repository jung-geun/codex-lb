import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { getSourceAccess } from "@/features/api-keys/api";
import type { SourceAccess } from "@/features/api-keys/schemas";
import { hasPermission, useAuthStore, usePermission } from "@/features/auth/hooks/use-auth";
import { ApiError } from "@/lib/api-client";

export function useSourceAccess(keyId?: string, enabled = true) {
  const queryClient = useQueryClient();
  const initialized = useAuthStore((state) => state.initialized);
  const canRead = usePermission("api_keys:read");
  const canReadAccounts = usePermission("accounts:read");
  const permitted = initialized && canRead && canReadAccounts;
  const queryKey = ["api-keys", "source-access", keyId ?? null];
  const query = useQuery<SourceAccess | null, Error>({
    queryKey,
    queryFn: async ({ signal }) => {
      const auth = useAuthStore.getState();
      if (!auth.initialized || !hasPermission(auth.permissions, "api_keys:read") ||
          !hasPermission(auth.permissions, "accounts:read")) {
        queryClient.setQueryData(queryKey, null, { updatedAt: 0 });
        throw new ApiError({ status: 403, code: "forbidden", message: "Source inventory access denied" });
      }
      try {
        return await getSourceAccess(keyId, signal);
      } catch (cause) {
        if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) {
          queryClient.setQueriesData({ queryKey: ["api-keys", "source-access"] }, null, { updatedAt: 0 });
        }
        throw cause;
      }
    },
    enabled: enabled && permitted,
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
  useEffect(() => {
    if (permitted) return;
    void queryClient.cancelQueries({ queryKey: ["api-keys", "source-access"] });
    queryClient.setQueriesData({ queryKey: ["api-keys", "source-access"] }, null, { updatedAt: 0 });
  }, [permitted, queryClient]);
  const denied = query.error instanceof ApiError && (query.error.status === 401 || query.error.status === 403);
  return { ...query, permitted, data: permitted && !denied ? query.data ?? undefined : undefined };
}
