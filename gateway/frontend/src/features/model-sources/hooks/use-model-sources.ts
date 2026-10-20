import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";
import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { hasPermission, useAuthStore, usePermission } from "@/features/auth/hooks/use-auth";
import { ApiError } from "@/lib/api-client";

import {
  createModelSource,
  deleteModelSource,
  listModelSources,
  getModelSourceUsage,
  getModelSourceApiUsage,
  getModelSourceQuotaHistory,
  updateModelSource,
} from "@/features/model-sources/api";
import type {
  ModelSource,
  ModelSourceCreateRequest,
  ModelSourceUpdateRequest,
  ModelSourcesResponse,
} from "@/features/model-sources/schemas";

type ModelSourceUpdateVariables = { sourceId: string; payload: ModelSourceUpdateRequest };

export type UseModelSourcesResult = {
  modelSourcesQuery: Pick<
    UseQueryResult<ModelSourcesResponse | null, Error>,
    "error" | "isFetching" | "isLoading" | "isPending" | "isSuccess" | "refetch"
  > & { data: ModelSourcesResponse | undefined };
  createMutation: UseMutationResult<ModelSource, Error, ModelSourceCreateRequest>;
  updateMutation: UseMutationResult<ModelSource, Error, ModelSourceUpdateVariables>;
  deleteMutation: UseMutationResult<void, Error, string>;
};

export function useModelSources(): UseModelSourcesResult {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const initialized = useAuthStore((state) => state.initialized);
  const hasAccountsRead = usePermission("accounts:read");
  const canRead = initialized && hasAccountsRead;

  const { data, error, isFetching, isLoading, isPending, isSuccess, refetch } = useQuery<ModelSourcesResponse | null, Error>({
    queryKey: ["model-sources", "list"],
    queryFn: async ({ signal }) => {
      const auth = useAuthStore.getState();
      if (!auth.initialized || !hasPermission(auth.permissions, "accounts:read")) {
        queryClient.setQueryData<ModelSourcesResponse | null>(["model-sources", "list"], null);
        throw new ApiError({ status: 403, code: "forbidden", message: t("sourceMonitoring.sourcesFailed") });
      }
      try {
        return await listModelSources(signal);
      } catch (cause) {
        if (signal.aborted) throw new Error(t("sourceMonitoring.sourcesFailed"), { cause });
        if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) {
          // Clear sensitive data in the shared cache, not just this observer.
          // A remount after a later transient failure must not revive it.
          queryClient.setQueryData<ModelSourcesResponse | null>(["model-sources", "list"], null);
        }
        if (cause instanceof ApiError) {
          throw new ApiError({ status: cause.status, code: cause.code, message: t("sourceMonitoring.sourcesFailed") }, { cause });
        }
        throw new Error(t("sourceMonitoring.sourcesFailed"), { cause });
      }
    },
    enabled: canRead,
    refetchInterval: 30_000,
  });
  const accessDenied = error instanceof ApiError && (error.status === 401 || error.status === 403);
  useEffect(() => {
    if (canRead) return;
    void queryClient.cancelQueries({ queryKey: ["model-sources", "list"], exact: true });
    // Permission restoration requires a new authorized read before disclosure.
    queryClient.setQueryData<ModelSourcesResponse | null>(["model-sources", "list"], null);
  }, [canRead, queryClient]);
  const modelSourcesQuery = {
    data: canRead && !accessDenied ? data ?? undefined : undefined,
    error,
    isFetching: canRead && isFetching,
    isLoading: canRead && isLoading,
    isPending: canRead && isPending,
    isSuccess: canRead && !accessDenied && data !== null && isSuccess,
    refetch,
  };

  const createMutation = useMutation({
    mutationFn: (payload: ModelSourceCreateRequest) => createModelSource(payload),
    onSuccess: (source) => {
      queryClient.setQueryData<ModelSourcesResponse | null>(["model-sources", "list"], (current) => current === null ? null : ({
        sources: [...(current?.sources ?? []).filter((item) => item.id !== source.id), source],
      }));
      toast.success(t("modelSources.toasts.created"));
      void queryClient.invalidateQueries({ queryKey: ["model-sources", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["api-keys", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (error: Error) => {
      toast.error(error.message || t("modelSources.toasts.createFailed"));
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ sourceId, payload }: ModelSourceUpdateVariables) =>
      updateModelSource(sourceId, payload),
    onSuccess: (source) => {
      toast.success(t("modelSources.toasts.updated"));
      void queryClient.invalidateQueries({ queryKey: ["model-sources", "list"] });
      void queryClient.resetQueries({ queryKey: ["model-sources", "quota-history", source.id] });
      void queryClient.resetQueries({ queryKey: ["model-sources", "api-usage", source.id] });
      void queryClient.invalidateQueries({ queryKey: ["api-keys", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["models"] });
      void queryClient.resetQueries({ queryKey: ["quota-estimates"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: (error: Error) => {
      toast.error(error.message || t("modelSources.toasts.updateFailed"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (sourceId: string) => deleteModelSource(sourceId),
    onSuccess: () => {
      toast.success(t("modelSources.toasts.deleted"));
      void queryClient.invalidateQueries({ queryKey: ["model-sources", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["api-keys", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["models"] });
      void queryClient.invalidateQueries({ queryKey: ["quota-estimates"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: (error: Error) => {
      toast.error(error.message || t("modelSources.toasts.deleteFailed"));
    },
  });

  return {
    modelSourcesQuery,
    createMutation,
    updateMutation,
    deleteMutation,
  };
}

export function useModelSourceUsage(sourceId: string | null, days: 1 | 7 | 30 = 1) {
  return useQuery({
    queryKey: ["model-sources", "usage", sourceId, days],
    queryFn: ({ signal }) => getModelSourceUsage(sourceId!, signal, days),
    enabled: sourceId !== null,
    refetchInterval: 30_000,
  });
}

export function useModelSourceApiUsage(sourceId: string | null) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const initialized = useAuthStore((state) => state.initialized);
  const canReadAccounts = usePermission("accounts:read");
  const enabled = sourceId !== null && initialized && canReadAccounts;
  const queryKey = ["model-sources", "api-usage", sourceId] as const;
  const query = useQuery({
    queryKey,
    queryFn: async ({ signal }) => {
      const auth = useAuthStore.getState();
      if (sourceId === null || !auth.initialized || !hasPermission(auth.permissions, "accounts:read")) {
        throw new ApiError({ status: 403, code: "forbidden", message: t("accounts.apiUsage.failed") });
      }
      try {
        return await getModelSourceApiUsage(sourceId, signal);
      } catch (cause) {
        if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) {
          queryClient.setQueryData(queryKey, null);
        }
        throw new Error(t("accounts.apiUsage.failed"), { cause });
      }
    },
    enabled,
    refetchInterval: 30_000,
  });
  useEffect(() => {
    if (enabled) return;
    void queryClient.cancelQueries({ queryKey: ["model-sources", "api-usage", sourceId], exact: true });
    queryClient.setQueryData(["model-sources", "api-usage", sourceId], null);
  }, [enabled, queryClient, sourceId]);
  return { ...query, data: enabled && !query.isError ? query.data ?? undefined : undefined, isPending: enabled && query.isPending };
}

export function useModelSourceQuotaHistory(sourceId: string | null) {
  return useQuery({
    queryKey: ["model-sources", "quota-history", sourceId, 7],
    queryFn: ({ signal }) => getModelSourceQuotaHistory(sourceId!, signal),
    enabled: sourceId !== null,
    refetchInterval: 30_000,
  });
}
