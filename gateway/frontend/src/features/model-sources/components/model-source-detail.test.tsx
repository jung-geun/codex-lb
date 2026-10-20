import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "@/test/mocks/server";

import { useAuthStore } from "@/features/auth/hooks/use-auth";
import { ModelSourceDetail } from "@/features/model-sources/components/model-source-detail";
import { ADMIN_PERMISSIONS, createModelSource } from "@/test/mocks/factories";
import { renderWithProviders } from "@/test/utils";

const source = createModelSource({
  id: "existing", name: "Synthetic Claude", provider: "claude", authType: "claude_oauth", billingMode: "subscription",
  entitlementStatus: "verified", quotaError: "Provider observation unavailable",
  quotaWindows: [{ windowKey: "seven_day", model: null, usedRatio: 1, usedValue: null, limitValue: null, resetsAt: null, observedAt: "2026-01-01T00:00:00Z" }],
});

beforeEach(() => {
  useAuthStore.setState({ initialized: true, permissions: ADMIN_PERMISSIONS, canWrite: true });
  server.use(http.get("/api/model-sources/:sourceId/api-usage", ({ params }) => HttpResponse.json({ sourceId: params.sourceId, observedAt: null, rateLimits: [], balances: [], balanceObservedAt: null, balanceStatus: "unavailable", balanceError: null, balanceIsAvailable: null })));
});
afterEach(() => useAuthStore.setState({ permissions: ADMIN_PERMISSIONS, canWrite: true }));

describe("ModelSourceDetail", () => {
  it("keeps unknown pricing, incomplete usage and actual quota observations in the common panels", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/model-sources/existing/usage", () => HttpResponse.json({
        sourceId: "existing", windowStart: "2026-01-01T00:00:00Z", windowEnd: "2026-01-02T00:00:00Z",
        requestCount: 3, successCount: 2, errorCount: 1, cancelledCount: 0, inputTokens: 1200, outputTokens: 50,
        cachedInputTokens: 100, cacheCreationInputTokens: 20, reasoningTokens: 10, costUsd: null, knownCostUsd: 0.25,
        unpricedRequestCount: 1, unknownUsageCount: 1, lastRequestAt: "2026-01-01T23:00:00Z",
      })),
      http.get("/api/model-sources/existing/quota-history", () => HttpResponse.json({
        sourceId: "existing", windowStart: "2026-01-01T00:00:00Z", windowEnd: "2026-01-08T00:00:00Z",
        windows: [
          { ...source.quotaWindows![0], usedRatio: null, observedAt: "2026-01-06T00:00:00Z" },
          { ...source.quotaWindows![0], usedRatio: 1, observedAt: "2026-01-05T03:42:17Z" },
        ],
      })),
    );
    renderWithProviders(<ModelSourceDetail source={source} readOnly />);
    expect(await screen.findByText(/1 requests are unpriced/)).toHaveTextContent("$0.25");
    expect(screen.getByText(/1 requests have unknown usage/)).toBeInTheDocument();
    const usage = screen.getByRole("region", { name: "Observed gateway usage · last 24 hours" });
    expect(usage).not.toHaveTextContent("$0.00");
    expect(usage).toHaveTextContent("Unknown");
    await user.click(await screen.findByText("Actual observations", { selector: "summary" }));
    const table = screen.getByRole("table", { name: "Actual observations" });
    expect(within(table).getByText("Unknown")).toBeInTheDocument();
    expect(within(table).getByText("0.0%")).toBeInTheDocument();
    expect([...table.querySelectorAll("time")].map((time) => time.dateTime)).toEqual(["2026-01-05T03:42:17Z", "2026-01-06T00:00:00Z"]);
  });

  it("does not claim missing history is empty or source quota is an active full allocation", async () => {
    server.use(http.get("/api/model-sources/existing/quota-history", () => HttpResponse.json({error:{code:"not_found",message:"private upstream details"}},{status:404})));
    renderWithProviders(<ModelSourceDetail source={{ ...source, quotaError: "Bearer private-secret", quotaWindows: [] }} readOnly />);
    expect(await screen.findByText("Could not load quota observations.")).toBeInTheDocument();
    expect(screen.queryByText("No quota observations recorded in this interval.")).not.toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.queryByText(/private-secret|private upstream details/)).not.toBeInTheDocument();
  });

  it("uses a provider email as its header while retaining the registration name", () => {
    renderWithProviders(<ModelSourceDetail source={{ ...source, accountLabel: "member@example.com" }} readOnly />);
    expect(screen.getByRole("heading", { name: "member@example.com" })).toBeInTheDocument();
    expect(screen.getByText(source.name)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: source.name })).not.toBeInTheDocument();
  });

  it.each(["openai", "anthropic"] as const)("shows checked %s API access without inventing subscription entitlement or balance", async (provider) => {
    const api = createModelSource({ provider, authType: "api_key", billingMode: "api_metered", healthStatus: "healthy", quotaCheckedAt: "2026-01-01T00:00:00Z", quotaError: null, quotaWindows: [], entitlementStatus: "unverified" });
    renderWithProviders(<ModelSourceDetail source={api} readOnly />);
    expect(screen.getByText("API key / model catalog checked")).toBeInTheDocument();
    expect(await screen.findByText(/OpenAI and Anthropic API balances cannot be read here/)).toBeInTheDocument();
    expect(screen.queryByText(/unverified/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Used 0%/)).not.toBeInTheDocument();
  });

  it("blocks fallback saves when source inventory refresh fails after the editor opens", async () => {
    const user = userEvent.setup();
    const api = createModelSource({ id: "guard-api", provider: "openai", authType: "api_key", billingMode: "api_metered" });
    const writes: string[] = [];
    server.use(http.patch("/api/model-sources/guard-api", ({ request }) => { writes.push(request.method); return HttpResponse.json(api); }));
    const { queryClient } = renderWithProviders(<ModelSourceDetail source={api} />);
    const edit = screen.getByRole("button", { name: /^Edit / });
    await waitFor(() => expect(edit).toBeEnabled());
    await user.click(edit);
    await user.click(screen.getByRole("switch", { name: "Use API only after subscriptions" }));
    server.use(http.get("/api/model-sources/", () => HttpResponse.json({ error: { code: "upstream_error", message: "private inventory error" } }, { status: 503 })));
    await act(async () => { await queryClient.refetchQueries({ queryKey: ["model-sources", "list"] }); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save" })).toBeDisabled());
    expect(screen.queryByText("private inventory error")).not.toBeInTheDocument();
    expect(writes).toEqual([]);
  });

  it("switches retained gateway usage between 1, 7 and 30 days without mutating a readonly source", async () => {
    const user = userEvent.setup();
    const daysSeen: number[] = [];
    const writes: string[] = [];
    const api = createModelSource({ id: "usage-api", provider: "deepseek", authType: "api_key", billingMode: "api_metered" });
    server.use(
      http.get("/api/model-sources/usage-api/usage", ({ request }) => {
        const days = Number(new URL(request.url).searchParams.get("days"));
        daysSeen.push(days);
        return HttpResponse.json({ sourceId: api.id, windowStart: "2026-01-01T00:00:00Z", windowEnd: "2026-01-02T00:00:00Z", requestCount: days, successCount: days, errorCount: 0, cancelledCount: 0, inputTokens: days * 100, outputTokens: 10, cachedInputTokens: 5, cacheCreationInputTokens: 2, reasoningTokens: 1, costUsd: null, knownCostUsd: null, unpricedRequestCount: days, unknownUsageCount: 0, lastRequestAt: null });
      }),
      http.patch("/api/model-sources/usage-api", ({ request }) => { writes.push(request.method); return HttpResponse.json(api); }),
      http.post("/api/model-sources/usage-api/quota", ({ request }) => { writes.push(request.method); return HttpResponse.json(api); }),
    );
    renderWithProviders(<ModelSourceDetail source={api} readOnly />);
    await waitFor(() => expect(daysSeen).toContain(1));
    await user.click(screen.getByRole("combobox", { name: "Gateway usage period" }));
    await user.click(screen.getByRole("option", { name: "7 days" }));
    await waitFor(() => expect(daysSeen).toContain(7));
    expect(await screen.findByRole("region", { name: "Observed gateway usage · last 7 days" })).toHaveTextContent("7 requests are unpriced");
    await user.click(screen.getByRole("combobox", { name: "Gateway usage period" }));
    await user.click(screen.getByRole("option", { name: "30 days" }));
    await waitFor(() => expect(daysSeen).toContain(30));
    expect(await screen.findByRole("region", { name: "Observed gateway usage · last 30 days" })).toHaveTextContent("30 requests are unpriced");
    expect(writes).toEqual([]);
  });

  it.each([
    { readOnly: true, permissions: ADMIN_PERMISSIONS, canWrite: true },
    { readOnly: false, permissions: ["accounts:read:all"], canWrite: true },
    { readOnly: false, permissions: ADMIN_PERMISSIONS, canWrite: false },
  ])("keeps monitoring but does not expose writes without both grants (%j)", ({ readOnly, permissions, canWrite }) => {
    useAuthStore.setState({ permissions, canWrite });
    renderWithProviders(<ModelSourceDetail source={source} readOnly={readOnly} />);
    expect(screen.getByRole("heading", { name: source.name })).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Weekly quota remaining" })).toHaveAttribute("value", "0");
    expect(screen.getByText(/Provider observation failed/)).toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Edit Synthetic|Delete Synthetic|Connect \/ import|Refresh entitlement/ })).not.toBeInTheDocument();
  });

  it("closes an open write dialog on access loss and does not revive it when access returns", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ModelSourceDetail source={source} />);
    const edit = screen.getByRole("button", { name: /^Edit Synthetic Claude/ });
    await waitFor(() => expect(edit).toBeEnabled());
    await user.click(edit);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    act(() => useAuthStore.setState({ permissions: ["accounts:read:all"], canWrite: false }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    act(() => useAuthStore.setState({ permissions: ADMIN_PERMISSIONS, canWrite: true }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: source.name })).toBeInTheDocument();
  });
});
