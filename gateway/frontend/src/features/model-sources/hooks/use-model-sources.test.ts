import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ADMIN_PERMISSIONS, createModelSource } from "@/test/mocks/factories";
import { useModelSourceApiUsage, useModelSourceQuotaHistory, useModelSources } from "@/features/model-sources/hooks/use-model-sources";
import { useAuthStore } from "@/features/auth/hooks/use-auth";
import { ApiError } from "@/lib/api-client";
import type { ModelSourceApiUsage, ModelSourceQuotaHistoryResponse } from "@/features/model-sources/schemas";

const modelSourceApiMocks = vi.hoisted(() => ({
  listModelSources: vi.fn(),
  createModelSource: vi.fn(),
  updateModelSource: vi.fn(),
  deleteModelSource: vi.fn(),
  getModelSourceUsage: vi.fn(),
  getModelSourceQuotaHistory: vi.fn(),
  getModelSourceApiUsage: vi.fn(),
}));

const toastMocks = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock("@/features/model-sources/api", () => modelSourceApiMocks);
vi.mock("sonner", () => ({ toast: toastMocks }));

function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
}

function createWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: PropsWithChildren) {
    return createElement(QueryClientProvider, { client: queryClient }, children);
  };
}

beforeEach(() => useAuthStore.setState({ initialized: true, permissions: ADMIN_PERMISSIONS, canWrite: true }));

afterEach(() => {
  vi.clearAllMocks();
  useAuthStore.setState({ initialized: false, permissions: [], canWrite: false });
});

const telemetry: ModelSourceApiUsage = { sourceId: "api", observedAt: null, rateLimits: [], balances: [], balanceObservedAt: null, balanceStatus: "unavailable", balanceError: null, balanceIsAvailable: null };

describe("useModelSourceApiUsage", () => {
  it("masks cached telemetry on read revocation and rejects retained refetch callbacks", async () => {
    const queryClient = createTestQueryClient();
    modelSourceApiMocks.getModelSourceApiUsage.mockResolvedValue(telemetry);
    const { result } = renderHook(() => useModelSourceApiUsage("api"), { wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(result.current.data).toEqual(telemetry));
    const refetch = result.current.refetch;
    const reads = modelSourceApiMocks.getModelSourceApiUsage.mock.calls.length;
    act(() => useAuthStore.setState({ permissions: ["api_keys:read:all"] }));
    expect(result.current.data).toBeUndefined();
    expect(queryClient.getQueryData(["model-sources", "api-usage", "api"])).toBeNull();
    await act(async () => { await refetch(); });
    expect(modelSourceApiMocks.getModelSourceApiUsage).toHaveBeenCalledTimes(reads);
    expect(result.current.data).toBeUndefined();
  });

  it("saves API fallback false and refreshes telemetry without retaining the previous principal's allowance", async () => {
    const queryClient = createTestQueryClient();
    const source = createModelSource({ id: "api", provider: "openai", billingMode: "api_metered", apiFallbackOnly: false });
    modelSourceApiMocks.listModelSources.mockResolvedValue({ sources: [source] });
    modelSourceApiMocks.updateModelSource.mockResolvedValue(source);
    modelSourceApiMocks.getModelSourceApiUsage.mockResolvedValueOnce(telemetry);
    const { result } = renderHook(() => ({ sources: useModelSources(), usage: useModelSourceApiUsage("api") }), { wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(result.current.usage.data).toEqual(telemetry));
    let finish!: (value: ModelSourceApiUsage) => void;
    modelSourceApiMocks.getModelSourceApiUsage.mockReturnValueOnce(new Promise<ModelSourceApiUsage>((resolve) => { finish = resolve; }));
    await act(async () => { await result.current.sources.updateMutation.mutateAsync({ sourceId: "api", payload: { apiFallbackOnly: false } }); });
    expect(modelSourceApiMocks.updateModelSource).toHaveBeenCalledWith("api", { apiFallbackOnly: false });
    await waitFor(() => expect(result.current.usage.isFetching).toBe(true));
    expect(result.current.usage.data).toBeUndefined();
    const fresh: ModelSourceApiUsage = { ...telemetry, observedAt: "2026-01-01T00:00:00Z" };
    await act(async () => { finish(fresh); });
    await waitFor(() => expect(result.current.usage.data).toEqual(fresh));
  });
});

describe("useModelSources", () => {
  it.each([
    { initialized: false, permissions: ADMIN_PERMISSIONS },
    { initialized: true, permissions: ["api_keys:read:all"] },
  ])("does not fetch or expose cached source inventory without initialized accounts read (%j)", async ({ initialized, permissions }) => {
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(["model-sources", "list"], { sources: [createModelSource({ name: "Private cached source" })] });
    useAuthStore.setState({ initialized, permissions });
    const { result } = renderHook(() => useModelSources(), { wrapper: createWrapper(queryClient) });
    expect(result.current.modelSourcesQuery.data).toBeUndefined();
    expect(result.current.modelSourcesQuery.isSuccess).toBe(false);
    expect(result.current.modelSourcesQuery.isPending).toBe(false);
    expect(modelSourceApiMocks.listModelSources).not.toHaveBeenCalled();
    await act(async () => { await result.current.modelSourcesQuery.refetch(); });
    expect(modelSourceApiMocks.listModelSources).not.toHaveBeenCalled();
    expect(result.current.modelSourcesQuery.data).toBeUndefined();
  });

  it("masks cached source identities immediately when accounts read is revoked", async () => {
    const queryClient = createTestQueryClient();
    const source = createModelSource({ name: "Private source" });
    modelSourceApiMocks.listModelSources.mockResolvedValue({ sources: [source] });
    const { result } = renderHook(() => useModelSources(), { wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(result.current.modelSourcesQuery.data?.sources).toEqual([source]));
    const reads = modelSourceApiMocks.listModelSources.mock.calls.length;
    act(() => useAuthStore.setState({ permissions: ["api_keys:read:all"] }));
    expect(result.current.modelSourcesQuery.data).toBeUndefined();
    expect(result.current.modelSourcesQuery.isSuccess).toBe(false);
    await act(async () => { await result.current.modelSourcesQuery.refetch(); });
    expect(modelSourceApiMocks.listModelSources).toHaveBeenCalledTimes(reads);
  });

  it.each([401, 403])("keeps source data hidden after backend %s until a successful authorized read", async (status) => {
    const queryClient = createTestQueryClient();
    const source = createModelSource({ name: "Private source" });
    modelSourceApiMocks.listModelSources.mockResolvedValue({ sources: [source] });
    const { result } = renderHook(() => useModelSources(), { wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(result.current.modelSourcesQuery.data?.sources).toEqual([source]));
    modelSourceApiMocks.listModelSources.mockRejectedValueOnce(new ApiError({ status, code: "forbidden", message: "Denied" }));
    await act(async () => { await result.current.modelSourcesQuery.refetch(); });
    await waitFor(() => expect(result.current.modelSourcesQuery.data).toBeUndefined());
    modelSourceApiMocks.listModelSources.mockRejectedValueOnce(new Error("Temporary connection failure"));
    await act(async () => { await result.current.modelSourcesQuery.refetch(); });
    expect(result.current.modelSourcesQuery.data).toBeUndefined();
    await act(async () => { await result.current.modelSourcesQuery.refetch(); });
    await waitFor(() => expect(result.current.modelSourcesQuery.data?.sources).toEqual([source]));
  });

  it("requires a completed authorized read after permission restoration, not a cached success flag", async () => {
    const queryClient = createTestQueryClient();
    const previous = createModelSource({ name: "Previous private source" });
    const current = createModelSource({ name: "Current authorized source" });
    modelSourceApiMocks.listModelSources.mockResolvedValueOnce({ sources: [previous] });
    const { result } = renderHook(() => useModelSources(), { wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(result.current.modelSourcesQuery.data?.sources).toEqual([previous]));
    act(() => useAuthStore.setState({ permissions: ["api_keys:read:all"] }));
    expect(result.current.modelSourcesQuery.data).toBeUndefined();
    expect(queryClient.getQueryData(["model-sources", "list"])).toBeNull();
    let finish!: (value: { sources: typeof current[] }) => void;
    modelSourceApiMocks.listModelSources.mockReturnValueOnce(new Promise<{ sources: typeof current[] }>((resolve) => { finish = resolve; }));
    act(() => useAuthStore.setState({ permissions: ADMIN_PERMISSIONS }));
    await waitFor(() => expect(modelSourceApiMocks.listModelSources).toHaveBeenCalledTimes(2));
    expect(result.current.modelSourcesQuery.data).toBeUndefined();
    expect(result.current.modelSourcesQuery.isSuccess).toBe(false);
    await act(async () => { finish({ sources: [current] }); });
    await waitFor(() => expect(result.current.modelSourcesQuery.data?.sources).toEqual([current]));
  });

  it.each([401, 403])("does not revive denied source identities after %s, a transient failure and remount", async (status) => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
    const previous = createModelSource({ name: "Denied private source" });
    const current = createModelSource({ name: "Fresh authorized source" });
    modelSourceApiMocks.listModelSources.mockResolvedValue({ sources: [previous] });
    const first = renderHook(() => useModelSources(), { wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(first.result.current.modelSourcesQuery.data?.sources).toEqual([previous]));
    modelSourceApiMocks.listModelSources.mockRejectedValueOnce(new ApiError({ status, code: "forbidden", message: "Bearer private-error" }));
    await act(async () => { await first.result.current.modelSourcesQuery.refetch(); });
    await waitFor(() => expect(first.result.current.modelSourcesQuery.error).toBeInstanceOf(ApiError));
    expect(first.result.current.modelSourcesQuery.error?.message).not.toContain("private-error");
    modelSourceApiMocks.listModelSources.mockRejectedValueOnce(new Error("Temporary failure"));
    await act(async () => { await first.result.current.modelSourcesQuery.refetch(); });
    expect(queryClient.getQueryData(["model-sources", "list"])).toBeNull();
    first.unmount();
    let finish!: (value: { sources: typeof current[] }) => void;
    modelSourceApiMocks.listModelSources.mockReturnValueOnce(new Promise<{ sources: typeof current[] }>((resolve) => { finish = resolve; }));
    const next = renderHook(() => useModelSources(), { wrapper: createWrapper(queryClient) });
    expect(next.result.current.modelSourcesQuery.data).toBeUndefined();
    expect(next.result.current.modelSourcesQuery.isSuccess).toBe(false);
    await act(async () => { finish({ sources: [current] }); });
    await waitFor(() => expect(next.result.current.modelSourcesQuery.data?.sources).toEqual([current]));
    queryClient.clear();
  });

  it("makes a newly registered source selectable while the list refresh is still pending", async () => {
    const queryClient = createTestQueryClient();
    const source = createModelSource({ id: "source_1", name: "Source One", provider: "openai", authType: "api_key", billingMode: "api_metered" });
    let resolveRefresh!: (value: { sources: typeof source[] }) => void;
    const refresh = new Promise<{ sources: typeof source[] }>((resolve) => { resolveRefresh = resolve; });
    modelSourceApiMocks.listModelSources.mockResolvedValueOnce({ sources: [] }).mockReturnValue(refresh);
    modelSourceApiMocks.createModelSource.mockResolvedValue(source);
    const { result } = renderHook(() => useModelSources(), { wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(result.current.modelSourcesQuery.isSuccess).toBe(true));
    await result.current.createMutation.mutateAsync({
      name: source.name, baseUrl: source.baseUrl, provider: "openai", authType: "api_key", billingMode: "api_metered", apiKey: "token", models: [],
    });
    await waitFor(() => expect(result.current.modelSourcesQuery.data?.sources).toEqual([source]));
    resolveRefresh({ sources: [source] });
  });

  it("hides previous credential history until the replacement principal's observations load", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
    const source = createModelSource({ id: "source_1" });
    const previous: ModelSourceQuotaHistoryResponse = {
      sourceId: source.id, windowStart: "2026-01-01T00:00:00Z", windowEnd: "2026-01-08T00:00:00Z",
      windows: [{ windowKey: "seven_day", model: null, usedRatio: 0.9, usedValue: null, limitValue: null,
        resetsAt: null, observedAt: "2026-01-07T12:00:00Z" }],
    };
    modelSourceApiMocks.listModelSources.mockResolvedValue({ sources: [source] });
    modelSourceApiMocks.updateModelSource.mockResolvedValue(source);
    modelSourceApiMocks.getModelSourceQuotaHistory.mockResolvedValueOnce(previous);
    const { result } = renderHook(() => ({
      sources: useModelSources(), history: useModelSourceQuotaHistory(source.id),
    }), { wrapper: createWrapper(queryClient) });
    await waitFor(() => expect(result.current.history.data).toEqual(previous));
    let finish!: (value: ModelSourceQuotaHistoryResponse) => void;
    modelSourceApiMocks.getModelSourceQuotaHistory.mockReturnValueOnce(
      new Promise<ModelSourceQuotaHistoryResponse>((resolve) => { finish = resolve; }),
    );
    await act(async () => {
      await result.current.sources.updateMutation.mutateAsync({ sourceId: source.id, payload: { apiKey: "replacement" } });
    });
    await waitFor(() => expect(result.current.history.isFetching).toBe(true));
    expect(result.current.history.data).toBeUndefined();
    const fresh = { ...previous, windows: [] };
    await act(async () => { finish(fresh); });
    await waitFor(() => expect(result.current.history.data).toEqual(fresh));
    queryClient.clear();
  });
});

describe("useModelSourceQuotaHistory", () => {
  it("does not fetch without a selected source", () => {
    const queryClient = createTestQueryClient();
    const { result } = renderHook(() => useModelSourceQuotaHistory(null), { wrapper: createWrapper(queryClient) });
    expect(result.current.fetchStatus).toBe("idle");
    expect(modelSourceApiMocks.getModelSourceQuotaHistory).not.toHaveBeenCalled();
  });

  it("keeps caches per source, forwards cancellation, and performs no mutations", async () => {
    const queryClient = createTestQueryClient();
    const interval = { windowStart: "2026-01-01T00:00:00Z", windowEnd: "2026-01-08T00:00:00Z", windows: [] };
    modelSourceApiMocks.getModelSourceQuotaHistory.mockImplementation(async (sourceId: string) => ({ ...interval, sourceId }));
    const { result, rerender } = renderHook(({ sourceId }) => useModelSourceQuotaHistory(sourceId), {
      initialProps: { sourceId: "source_1" }, wrapper: createWrapper(queryClient),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(modelSourceApiMocks.getModelSourceQuotaHistory).toHaveBeenCalledWith("source_1", expect.any(AbortSignal));
    expect(queryClient.getQueryData(["model-sources", "quota-history", "source_1", 7])).toEqual({ ...interval, sourceId: "source_1" });

    rerender({ sourceId: "source_2" });
    await waitFor(() => expect(result.current.data?.sourceId).toBe("source_2"));
    expect(queryClient.getQueryData(["model-sources", "quota-history", "source_2", 7])).toEqual({ ...interval, sourceId: "source_2" });
    expect(modelSourceApiMocks.createModelSource).not.toHaveBeenCalled();
    expect(modelSourceApiMocks.updateModelSource).not.toHaveBeenCalled();
    expect(modelSourceApiMocks.deleteModelSource).not.toHaveBeenCalled();
  });
});
