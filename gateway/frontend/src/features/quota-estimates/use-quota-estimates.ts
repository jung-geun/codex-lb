import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { grantedScope, useAuthStore } from "@/features/auth/hooks/use-auth";
import { QuotaEstimatesResponseSchema, type QuotaEstimatesResponse } from "@/features/quota-estimates/schemas";
import { ApiError, get } from "@/lib/api-client";

const QUERY_KEY = ["quota-estimates"] as const;

export function useQuotaEstimates() {
  const queryClient = useQueryClient();
  const canRead = useAuthStore((state) => state.initialized && grantedScope(state.permissions, "accounts:read") === "all");
  const query = useQuery<QuotaEstimatesResponse | null, Error>({
    queryKey: QUERY_KEY,
    enabled: canRead,
    staleTime: 30_000,
    refetchInterval: 30_000,
    queryFn: async ({ signal }) => {
      const auth = useAuthStore.getState();
      if (!auth.initialized || grantedScope(auth.permissions, "accounts:read") !== "all") {
        queryClient.setQueryData(QUERY_KEY, null);
        throw new ApiError({ status: 403, code: "forbidden", message: "Quota estimates unavailable" });
      }
      try {
        return await get("/api/quota-estimates/", QuotaEstimatesResponseSchema, { signal });
      } catch (error) {
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
          queryClient.setQueryData(QUERY_KEY, null);
        }
        throw error;
      }
    },
  });
  useEffect(() => {
    if (canRead) {
      if (queryClient.getQueryData(QUERY_KEY) === null) void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      return;
    }
    void queryClient.cancelQueries({ queryKey: QUERY_KEY });
    queryClient.setQueryData(QUERY_KEY, null);
  }, [canRead, queryClient]);
  const state: "loading" | "error" | "ready" | undefined = !canRead ? undefined : query.isError ? "error" : query.isPending ? "loading" : "ready";
  return { ...query, canRead, state, data: canRead && !query.isError ? query.data ?? undefined : undefined };
}
