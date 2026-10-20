import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAuthStore } from "@/features/auth/hooks/use-auth";
import type { SourceAccess } from "@/features/api-keys/schemas";
import { ADMIN_PERMISSIONS } from "@/test/mocks/factories";
import { useSourceAccess } from "./use-source-access";
import { ApiError } from "@/lib/api-client";

const mocks = vi.hoisted(() => ({ getSourceAccess: vi.fn() }));
vi.mock("@/features/api-keys/api", () => mocks);

const inventory: SourceAccess = { models: [{
  sourceId: "private-source", sourceName: "Private Claude", provider: "claude", billingMode: "subscription",
  model: "claude-model", displayName: "Claude", availability: "available", quotaWindows: [],
}] };
const clients: QueryClient[] = [];

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

beforeEach(() => {
  mocks.getSourceAccess.mockReset().mockResolvedValue(inventory);
  useAuthStore.setState({ initialized: true, role: "admin", permissions: ADMIN_PERMISSIONS, canWrite: true });
});
afterEach(() => {
  for (const client of clients) client.clear();
  clients.length = 0;
  act(() => useAuthStore.setState({ initialized: false, permissions: [], canWrite: false }));
});

describe("source access management permissions", () => {
  it.each([
    { initialized: false, permissions: ADMIN_PERMISSIONS },
    { initialized: true, permissions: ["api_keys:read:all"] },
    { initialized: true, permissions: ["accounts:read:all"] },
  ])("hides cached source accounts without both initialized read permissions (%j)", (access) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
    clients.push(client);
    client.setQueryData(["api-keys", "source-access", null], inventory);
    useAuthStore.setState(access);
    const { result } = renderHook(() => useSourceAccess(), { wrapper: wrapperFor(client) });
    expect(result.current.data).toBeUndefined();
    expect(result.current.permitted).toBe(false);
    expect(mocks.getSourceAccess).not.toHaveBeenCalled();
  });

  it("does not reveal old identities while permission restoration is waiting for a new read", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
    clients.push(client);
    const { result } = renderHook(() => useSourceAccess(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.data).toEqual(inventory));
    act(() => useAuthStore.setState({ permissions: ["api_keys:read:all"] }));
    expect(result.current.data).toBeUndefined();
    let finish!: (value: SourceAccess) => void;
    mocks.getSourceAccess.mockReturnValueOnce(new Promise<SourceAccess>((resolve) => { finish = resolve; }));
    act(() => useAuthStore.setState({ permissions: ADMIN_PERMISSIONS }));
    await waitFor(() => expect(result.current.isFetching).toBe(true));
    expect(result.current.data).toBeUndefined();
    const fresh = { models: [{ ...inventory.models[0], sourceName: "Current authorized Claude" }] };
    await act(async () => { finish(fresh); });
    await waitFor(() => expect(result.current.data).toEqual(fresh));
  });

  it.each([401, 403])("does not revive source identities after denial %s, a transient failure and remount", async (status) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
    clients.push(client);
    const first = renderHook(() => useSourceAccess(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(first.result.current.data).toEqual(inventory));
    mocks.getSourceAccess.mockRejectedValueOnce(new ApiError({ status, code: "denied", message: "Denied" }));
    await act(async () => { await first.result.current.refetch(); });
    await waitFor(() => expect(first.result.current.data).toBeUndefined());
    mocks.getSourceAccess.mockRejectedValueOnce(new Error("Temporary failure"));
    await act(async () => { await first.result.current.refetch(); });
    expect(first.result.current.data).toBeUndefined();
    first.unmount();
    let finish!: (value: SourceAccess) => void;
    mocks.getSourceAccess.mockReturnValueOnce(new Promise<SourceAccess>((resolve) => { finish = resolve; }));
    const next = renderHook(() => useSourceAccess(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(next.result.current.isFetching).toBe(true));
    expect(next.result.current.data).toBeUndefined();
    await act(async () => { finish(inventory); });
    await waitFor(() => expect(next.result.current.data).toEqual(inventory));
  });
});
