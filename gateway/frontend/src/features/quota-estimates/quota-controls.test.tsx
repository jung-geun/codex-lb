import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";

import { AccountActions } from "@/features/accounts/components/account-actions";
import { sourceToAccountView } from "@/features/accounts/source-selection";
import { useAuthStore } from "@/features/auth/hooks/use-auth";
import { QuotaEstimateSchema } from "@/features/quota-estimates/schemas";
import { useQuotaEstimates } from "@/features/quota-estimates/use-quota-estimates";
import { createModelSource, ADMIN_PERMISSIONS } from "@/test/mocks/factories";
import { server } from "@/test/mocks/server";
import { createTestQueryClient } from "@/test/utils";

beforeEach(() => useAuthStore.setState({ initialized: true, permissions: ADMIN_PERMISSIONS, canWrite: true }));
afterEach(() => useAuthStore.setState({ initialized: false, permissions: [], canWrite: false }));

const evidence = QuotaEstimateSchema.parse({
  windowKey: "secondary", model: null, status: "estimated", reason: "valid",
  estimatedTotalTokens: 1_000_000, remainingTokens: 0, observedTokens: 100_000,
  sampleCount: 2, observationCount: 4, requestCount: 3, usedRatioDelta: 0.1,
  evidenceStart: "2026-01-01T00:00:00Z", evidenceEnd: "2026-01-01T01:00:00Z",
  currentObservedAt: "2026-01-01T01:00:00Z", resetsAt: "2026-01-08T00:00:00Z",
  currentUsedRatio: 1, basis: "observed_gateway_input_plus_output_tokens", estimateKind: "empirical_estimate",
  totalTokensLow: 900_000, totalTokensHigh: 1_100_000, caveats: [],
});

describe("quota allocation monitoring", () => {

  it("purges estimates on denied access and cannot revive them after a transient failure", async () => {
    const client = createTestQueryClient();
    server.use(http.get("/api/quota-estimates/", () => HttpResponse.json({ accounts: [{ accountId: "private-account", sharedAccountIds: ["private-account"], windows: [evidence] }], sources: [] })));
    const hook = renderHook(() => useQuotaEstimates(), { wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
    await waitFor(() => expect(hook.result.current.data?.accounts[0].accountId).toBe("private-account"));
    server.use(http.get("/api/quota-estimates/", () => HttpResponse.json({ error: { code: "forbidden", message: "Private error" } }, { status: 403 })));
    await act(async () => { await hook.result.current.refetch(); });
    await waitFor(() => expect(hook.result.current.data).toBeUndefined());
    expect(client.getQueryData(["quota-estimates"])).toBeNull();
    server.use(http.get("/api/quota-estimates/", () => HttpResponse.error()));
    await act(async () => { await hook.result.current.refetch(); });
    expect(hook.result.current.data).toBeUndefined();
    act(() => useAuthStore.setState({ permissions: ["accounts:read:own"] }));
    expect(hook.result.current.canRead).toBe(false);
    expect(hook.result.current.data).toBeUndefined();
    client.clear();
  });
});

describe("Claude source management", () => {
  it("allows opt-in but forbids a manual request while weekly quota is exhausted", async () => {
    const user = userEvent.setup();
    const onWarmupNow = vi.fn();
    const source = createModelSource({ provider: "claude", authType: "claude_oauth", billingMode: "subscription", warmupSupported: true, warmupUnavailableReason: "weekly_exhausted", limitWarmupEnabled: true });
    render(<AccountActions account={sourceToAccountView(source)} busy={false} onPause={vi.fn()} onResume={vi.fn()} onDelete={vi.fn()} onLimitWarmupChange={vi.fn()} onRoutingPolicyChange={vi.fn()} onWarmupNow={onWarmupNow} />);
    const manual = screen.getByRole("button", { name: "Warm up now" });
    expect(manual).toBeDisabled();
    await user.click(manual);
    expect(onWarmupNow).not.toHaveBeenCalled();
    expect(screen.getByText("Weekly quota exhausted; warm-up will not send a request")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Force probe|Export|Reset/ })).not.toBeInTheDocument();
  });
});
