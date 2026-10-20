import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DashboardPage } from "./dashboard-page";
import { useAuthStore } from "@/features/auth/hooks/use-auth";
import { listModelSources } from "@/features/model-sources/api";
import type * as ModelSourcesApi from "@/features/model-sources/api";
import type { ModelSource } from "@/features/model-sources/schemas";
import { useDashboardPreferencesStore } from "@/hooks/use-dashboard-preferences";
import { usePrivacyStore } from "@/hooks/use-privacy";
import { ApiError } from "@/lib/api-client";
import { ADMIN_PERMISSIONS, VIEWER_PERMISSIONS, createAccountSummary, createDashboardOverview, createModelSource } from "@/test/mocks/factories";
import { renderWithProviders } from "@/test/utils";

const { mutationsMock, dashboardMock, projectionsMock, conversationsMock, logsMock } = vi.hoisted(() => ({
  mutationsMock: vi.fn(), dashboardMock: vi.fn(), projectionsMock: vi.fn(), conversationsMock: vi.fn(), logsMock: vi.fn(),
}));
vi.mock("@/features/accounts/hooks/use-accounts", () => ({ useAccountMutations: mutationsMock }));
vi.mock("@/features/dashboard/hooks/use-dashboard", () => ({ useDashboard: dashboardMock, useDashboardProjections: projectionsMock }));
vi.mock("@/features/dashboard/hooks/use-conversations", () => ({ useConversations: conversationsMock }));
vi.mock("@/features/dashboard/hooks/use-request-logs", () => ({ useRequestLogs: logsMock }));
vi.mock("@/features/model-sources/api", async (importOriginal) => ({
  ...await importOriginal<typeof ModelSourcesApi>(), listModelSources: vi.fn(),
}));
vi.mock("@/features/dashboard/components/recent-requests-table", () => ({ RecentRequestsTable: () => null }));
vi.mock("@/features/dashboard/components/filters/request-filters", () => ({ RequestFilters: () => null }));
vi.mock("@/features/dashboard/components/stats-grid", () => ({ StatsGrid: () => null }));
vi.mock("@/features/dashboard/components/weekly-credits-pace-card", () => ({ WeeklyCreditsPaceCard: () => null }));
vi.mock("@/features/dashboard/components/usage-donuts", () => ({
  UsageDonuts: ({ primaryTotal, secondaryTotal }: { primaryTotal: number; secondaryTotal: number }) => (
    <div data-testid="codex-credit-capacity">{primaryTotal} / {secondaryTotal}</div>
  ),
}));

const listSourcesMock = vi.mocked(listModelSources);
const resume = vi.fn();
const warmup = vi.fn();

function source(overrides: Partial<ModelSource> = {}) {
  return createModelSource({
    id: "claude/account & personal", name: "Personal Claude", accountLabel: "claude@example.com",
    provider: "claude", authType: "claude_oauth", billingMode: "subscription", subscriptionType: "team",
    entitlementStatus: "verified", healthStatus: "healthy",
    quotaWindows: [
      { windowKey: "five_hour", model: null, usedRatio: 0.25, usedValue: null, limitValue: null, observedAt: new Date().toISOString(), resetsAt: null },
      { windowKey: "seven_day", model: null, usedRatio: 0.5, usedValue: null, limitValue: null, observedAt: new Date().toISOString(), resetsAt: null },
    ], ...overrides,
  });
}

beforeEach(() => {
  window.history.replaceState(null, "", "/dashboard");
  useAuthStore.setState({ initialized: true, permissions: ADMIN_PERMISSIONS, canWrite: true });
  usePrivacyStore.setState({ blurred: false });
  useDashboardPreferencesStore.setState({ initialized: true, accountViewMode: "cards", accountListSort: null });
  resume.mockReset();
  warmup.mockReset();
  listSourcesMock.mockReset();
  listSourcesMock.mockResolvedValue({ sources: [source()] });
  mutationsMock.mockReturnValue({
    resumeMutation: { mutateAsync: resume }, limitWarmupMutation: { mutateAsync: warmup },
  });
  dashboardMock.mockReturnValue({
    data: createDashboardOverview({ accounts: [createAccountSummary({ email: "codex@example.com", displayName: "Codex alias" })] }),
    isPending: false, isFetching: false, error: null,
  });
  projectionsMock.mockReturnValue({ data: undefined, isFetching: false });
  conversationsMock.mockReturnValue({
    conversationsQuery: { isFetching: false },
  });
  logsMock.mockReturnValue({
    filters: { search: "", timeframe: "all", accountIds: [], apiKeyIds: [], modelOptions: [], statuses: [], limit: 25, offset: 0 },
    logsQuery: { data: { requests: [], total: 0, hasMore: false }, error: null, isFetching: false, isSuccess: true },
    optionsQuery: { data: { accountIds: [], apiKeys: [], modelOptions: [], statuses: [] }, error: null },
    updateFilters: vi.fn(),
  });
});

afterEach(() => {
  usePrivacyStore.setState({ blurred: false });
  vi.restoreAllMocks();
});

function sourceCard() {
  return screen.getByText("claude@example.com").closest(".card-hover") as HTMLElement;
}

function sourceRow() {
  return screen.getByText("claude@example.com").closest('[data-testid="account-list-row"]') as HTMLElement;
}

describe("Dashboard shared accounts", () => {
  it("renders every provider in the same grid and list with combined status counts and unchanged Codex credits", async () => {
    listSourcesMock.mockResolvedValue({ sources: [source(), source({ id: "api", name: "Metered API", accountLabel: null, provider: "openai", authType: "api_key", billingMode: "api_metered", healthStatus: "error", quotaWindows: [] })] });
    renderWithProviders(<DashboardPage />);
    await screen.findByText("claude@example.com");
    const cards = screen.getByTestId("dashboard-account-cards");
    expect(within(cards).getByText("codex@example.com")).toBeInTheDocument();
    expect(within(cards).getByText("Metered API")).toBeInTheDocument();
    expect(within(sourceCard()).getByText("75%")).toBeInTheDocument();
    expect(within(sourceCard()).getByText("50%")).toBeInTheDocument();
    expect(within(sourceCard()).getByText("Personal Claude")).toBeInTheDocument();
    expect(within(sourceCard()).getAllByRole("button")).toHaveLength(1);
    expect(within(sourceCard()).queryByText(/Subscription quota|Purchased credits|Warm-up/)).not.toBeInTheDocument();
    expect(screen.getByTestId("dashboard-account-summary-line")).toHaveTextContent(/3.*registered.*2.*active.*1.*unavailable/);
    expect(screen.queryByRole("heading", { name: "Claude subscription accounts" })).not.toBeInTheDocument();
    expect(screen.getByTestId("codex-credit-capacity")).toHaveTextContent("225 / 7560");

    await userEvent.setup().click(screen.getByRole("radio", { name: "View accounts as list" }));
    expect(screen.queryByTestId("dashboard-account-cards")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("account-list-row")).toHaveLength(3);
    expect(within(sourceRow()).getByText("75%")).toBeInTheDocument();
    expect(within(sourceRow()).getByText("50%")).toBeInTheDocument();
    expect(within(sourceRow()).getAllByRole("button")).toHaveLength(1);
    expect(within(sourceRow()).queryByText("0.00")).not.toBeInTheDocument();
    expect(screen.getByTestId("dashboard-account-summary-line")).toHaveTextContent(/3.*registered.*2.*active.*1.*unavailable/);
    expect(screen.getByTestId("codex-credit-capacity")).toHaveTextContent("225 / 7560");
  });

  it.each([
    { mode: "cards", state: "paused" }, { mode: "list", state: "paused" },
    { mode: "cards", state: "reauth" }, { mode: "list", state: "reauth" },
    { mode: "cards", state: "blocked" }, { mode: "list", state: "blocked" },
  ] as const)("navigates source details without Codex mutations ($mode / $state)", async ({ mode, state }) => {
    useDashboardPreferencesStore.setState({ accountViewMode: mode });
    listSourcesMock.mockResolvedValue({ sources: [source({ isEnabled: state !== "paused", entitlementStatus: state === "reauth" ? "reauth_required" : state === "blocked" ? "blocked" : "verified" })] });
    renderWithProviders(<DashboardPage />);
    await screen.findByText("claude@example.com");
    const area = mode === "cards" ? sourceCard() : sourceRow();
    await userEvent.setup().click(within(area).getByRole("button", { name: /details/i }));
    expect(new URLSearchParams(window.location.search).get("selected")).toBe("source:claude/account & personal");
    expect(window.location.pathname).toBe("/accounts");
    expect(resume).not.toHaveBeenCalled();
    expect(warmup).not.toHaveBeenCalled();
  });

  it("keeps missing and failed source inventory visibly incomplete, retries safely, and labels cached failure", async () => {
    listSourcesMock.mockRejectedValueOnce(new Error("private upstream token detail"));
    const { queryClient } = renderWithProviders(<DashboardPage />);
    await screen.findByRole("alert");
    expect(screen.queryByText(/private upstream token detail/)).not.toBeInTheDocument();
    expect(screen.getByText("codex@example.com")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("claude@example.com");
    listSourcesMock.mockRejectedValueOnce(new Error("network down"));
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ["model-sources", "list"] }); });
    await screen.findByRole("alert");
    expect(screen.getByText(/cached|last loaded/i)).toBeInTheDocument();
    expect(screen.getByText("claude@example.com")).toBeInTheDocument();
  });

  it.each([401, 403])("purges cached identities after HTTP %s and does not revive them on later failure", async (status) => {
    const { queryClient } = renderWithProviders(<DashboardPage />);
    await screen.findByText("claude@example.com");
    listSourcesMock.mockRejectedValueOnce(new ApiError({ status, code: "forbidden", message: "private identity" }));
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ["model-sources", "list"] }); });
    await waitFor(() => expect(screen.queryByText("claude@example.com")).not.toBeInTheDocument());
    expect(screen.queryByText("Personal Claude")).not.toBeInTheDocument();
    expect(queryClient.getQueryData(["model-sources", "list"])).toBeNull();
    listSourcesMock.mockRejectedValueOnce(new Error("network down"));
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled());
    expect(screen.queryByText("claude@example.com")).not.toBeInTheDocument();
    expect(screen.getByTestId("dashboard-account-summary-line")).toHaveTextContent(/1.*registered/);
  });

  it("removes cached names and aliases immediately on read loss and requires a new authorized read", async () => {
    const { queryClient } = renderWithProviders(<DashboardPage />);
    await screen.findByText("claude@example.com");
    act(() => useAuthStore.setState({ permissions: ["dashboard:read:all"] }));
    expect(screen.queryByText("claude@example.com")).not.toBeInTheDocument();
    expect(screen.queryByText("Personal Claude")).not.toBeInTheDocument();
    await waitFor(() => expect(queryClient.getQueryData(["model-sources", "list"])).toBeNull());
    listSourcesMock.mockRejectedValueOnce(new Error("unavailable"));
    act(() => useAuthStore.setState({ permissions: ADMIN_PERMISSIONS }));
    await screen.findByRole("alert");
    expect(screen.queryByText("claude@example.com")).not.toBeInTheDocument();
  });

  it.each([false, true])("does not request sources before auth readiness/read permission (initialized=%s)", async (initialized) => {
    useAuthStore.setState({ initialized, permissions: initialized ? ["dashboard:read:all"] : ADMIN_PERMISSIONS });
    renderWithProviders(<DashboardPage />);
    expect(listSourcesMock).not.toHaveBeenCalled();
    expect(screen.queryByText("claude@example.com")).not.toBeInTheDocument();
  });

  it("ages source status on refetch even when the returned inventory is structurally unchanged", async () => {
    const inventory = { sources: [source()] };
    listSourcesMock.mockResolvedValue(inventory);
    const { queryClient } = renderWithProviders(<DashboardPage />);
    await screen.findByText("claude@example.com");
    expect(screen.getByTestId("dashboard-account-summary-line")).toHaveTextContent(/2.*registered.*2.*active.*0.*unavailable/);
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 60 * 60 * 1000);
    let resolveInventory!: (value: { sources: ModelSource[] }) => void;
    listSourcesMock.mockReturnValueOnce(new Promise((resolve) => { resolveInventory = resolve; }));
    let refetch: Promise<void> | undefined;
    act(() => { refetch = queryClient.invalidateQueries({ queryKey: ["model-sources", "list"] }); });
    await waitFor(() => expect(screen.getByTestId("dashboard-account-summary-line")).toHaveTextContent(/2.*registered.*1.*active.*1.*unavailable/));
    await act(async () => { resolveInventory(inventory); await refetch; });
    expect(screen.getByTestId("dashboard-account-summary-line")).toHaveTextContent(/2.*registered.*1.*active.*1.*unavailable/);
  });

  it.each(["cards", "list"] as const)("counts unknown, stale and failed sources as unavailable in %s", async (mode) => {
    useDashboardPreferencesStore.setState({ accountViewMode: mode });
    const stale = source({ id: "stale", accountLabel: "stale@example.com" });
    stale.quotaWindows = (stale.quotaWindows ?? []).map((window) => ({ ...window, observedAt: "2020-01-01T00:00:00Z" }));
    listSourcesMock.mockResolvedValue({ sources: [source({ quotaWindows: [] }), stale, source({ id: "failed", accountLabel: "failed@example.com", healthStatus: "error" })] });
    renderWithProviders(<DashboardPage />);
    await screen.findByText("claude@example.com");
    expect(screen.getByTestId("dashboard-account-summary-line")).toHaveTextContent(/4.*registered.*1.*active.*3.*unavailable/);
    const area = mode === "cards" ? sourceCard() : sourceRow();
    expect(within(area).queryByText("Active")).not.toBeInTheDocument();
    expect(within(area).queryByText("100%")).not.toBeInTheDocument();
  });

  it.each(["Subscription", "Purchased"])("sorts sources with unknown %s credits after known Codex balances in either direction", async (header) => {
    useDashboardPreferencesStore.setState({ accountViewMode: "list" });
    renderWithProviders(<DashboardPage />);
    await screen.findByText("claude@example.com");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: header }));
    expect(screen.getAllByTestId("account-list-row").at(-1)).toHaveTextContent("claude@example.com");
    await user.click(screen.getByRole("button", { name: `${header}, sorted ascending` }));
    expect(screen.getAllByTestId("account-list-row").at(-1)).toHaveTextContent("claude@example.com");
    expect(within(sourceRow()).queryByText("0.00")).not.toBeInTheDocument();
  });

  it("honors source identity privacy in both modes and keeps read-only source details usable", async () => {
    useAuthStore.setState({ permissions: VIEWER_PERMISSIONS, canWrite: false });
    usePrivacyStore.setState({ blurred: true });
    renderWithProviders(<DashboardPage />);
    await screen.findByText("claude@example.com");
    expect(screen.getByText("claude@example.com")).toHaveClass("privacy-blur");
    expect(screen.getByText("Personal Claude")).toHaveClass("privacy-blur");
    await userEvent.setup().click(screen.getByRole("radio", { name: "View accounts as list" }));
    const row = sourceRow();
    expect(within(row).getByText("claude@example.com")).toHaveClass("privacy-blur");
    expect(within(row).getByText("Personal Claude")).toHaveClass("privacy-blur");
    expect(within(row).getByRole("button", { name: "Details" })).toBeEnabled();
    expect(within(row).queryByRole("button", { name: /claude@example.com/ })).not.toBeInTheDocument();
  });
});
