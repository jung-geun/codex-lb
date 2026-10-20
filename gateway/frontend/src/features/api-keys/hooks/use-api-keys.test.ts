import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { createElement, type PropsWithChildren } from "react";
import { describe, expect, it, vi } from "vitest";

import { useApiKeys } from "@/features/api-keys/hooks/use-api-keys";
import { createApiKey, createApiKeyCreateResponse } from "@/test/mocks/factories";
import { server } from "@/test/mocks/server";

function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
      },
    },
  });
}

function createWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: PropsWithChildren) {
    return createElement(QueryClientProvider, { client: queryClient }, children);
  };
}

describe("useApiKeys", () => {
  it.each(["any", "subscription_only"] as const)("sends %s billing policy through the POST and PATCH client schemas", async (billingPolicy) => {
    const postBody = vi.fn();
    const patchBody = vi.fn();
    const assignedSourceIds = ["src_subscription"];
    const key = createApiKey({
      id: "key_policy",
      billingPolicy,
      sourceAssignmentScopeEnabled: true,
      assignedSourceIds,
    });
    server.use(
      http.post("/api/api-keys/", async ({ request }) => {
        postBody(await request.json());
        return HttpResponse.json(createApiKeyCreateResponse(key));
      }),
      http.patch("/api/api-keys/key_policy", async ({ request }) => {
        patchBody(await request.json());
        return HttpResponse.json(key);
      }),
    );

    const queryClient = createTestQueryClient();
    const { result } = renderHook(() => useApiKeys(), { wrapper: createWrapper(queryClient) });

    const created = await result.current.createMutation.mutateAsync({
      name: "Policy key",
      billingPolicy,
      assignedSourceIds,
    });
    expect(postBody).toHaveBeenCalledWith({ name: "Policy key", billingPolicy, assignedSourceIds });
    expect(created.billingPolicy).toBe(billingPolicy);
    expect(created.assignedSourceIds).toEqual(assignedSourceIds);

    const updated = await result.current.updateMutation.mutateAsync({
      keyId: key.id,
      payload: { billingPolicy },
    });
    expect(patchBody).toHaveBeenLastCalledWith({ billingPolicy });
    expect(updated.billingPolicy).toBe(billingPolicy);
    expect(updated.sourceAssignmentScopeEnabled).toBe(true);
    expect(updated.assignedSourceIds).toEqual(assignedSourceIds);

    await result.current.updateMutation.mutateAsync({
      keyId: key.id,
      payload: { billingPolicy, assignedSourceIds },
    });
    expect(patchBody).toHaveBeenLastCalledWith({ billingPolicy, assignedSourceIds });
  });

  it("runs CRUD mutations and returns plain key on create", async () => {
    const queryClient = createTestQueryClient();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useApiKeys(), {
      wrapper: createWrapper(queryClient),
    });

    await waitFor(() => expect(result.current.apiKeysQuery.isSuccess).toBe(true));
    const existingKeyId = result.current.apiKeysQuery.data?.[0]?.id;
    expect(existingKeyId).toBeTruthy();
    if (!existingKeyId) throw new Error("Expected at least one API key in test data");

    const created = await result.current.createMutation.mutateAsync({
      name: "Test Key",
      allowedModels: ["gpt-5.1"],
      applyToCodexModel: true,
      weeklyTokenLimit: 1000,
      expiresAt: null,
    });
    expect(created.key).toContain("sk-test");

    await result.current.updateMutation.mutateAsync({
      keyId: existingKeyId,
      payload: {
        name: "Updated Name",
      },
    });

    await result.current.regenerateMutation.mutateAsync(existingKeyId);
    await result.current.deleteMutation.mutateAsync(created.id);

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["api-keys", "list"] });
    });
  });
});
