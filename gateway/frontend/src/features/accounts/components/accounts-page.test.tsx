import { render as renderUi, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";

import { AccountsPage } from "@/features/accounts/components/accounts-page";
import { useAuthStore } from "@/features/auth/hooks/use-auth";
import { useAccountQuotaDisplayStore } from "@/hooks/use-account-quota-display";
import { ADMIN_PERMISSIONS, createModelSource, createUpstreamProxyAdmin } from "@/test/mocks/factories";
import type { AccountSummary } from "@/features/accounts/schemas";
import { renderWithProviders } from "@/test/utils";
import { server } from "@/test/mocks/server";

function render(ui: ReactElement) {
  return renderUi(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>{ui}</QueryClientProvider>);
}

vi.mock("@/features/accounts/hooks/use-accounts", () => ({
  useAccounts: vi.fn(),
  useAccountTrends: vi.fn(() => ({ data: null })),
  useAccountUsageResetCredits: vi.fn(() => ({
    data: { rateLimitResetCredits: { availableCount: 3 } },
    isFetching: false,
    error: null,
  })),
}));

vi.mock("@/features/accounts/hooks/use-oauth", () => ({
  useOauth: vi.fn(() => ({
    state: {
      status: "idle",
      method: null,
      authorizationUrl: null,
      callbackUrl: null,
      verificationUrl: null,
      userCode: null,
      deviceAuthId: null,
      intervalSeconds: null,
      expiresInSeconds: null,
      errorMessage: null,
    },
    start: vi.fn(),
    complete: vi.fn(),
    manualCallback: vi.fn(),
    reset: vi.fn(),
  })),
}));

vi.mock("@/features/settings/hooks/use-settings", () => ({
  useSettings: vi.fn(() => ({
    settingsQuery: {
      data: {
        showResetCreditBadges: true,
        showResetCreditExpiryBadge: true,
      },
      error: null,
    },
  })),
  useUpstreamProxyAdmin: vi.fn(() => ({
    upstreamProxyQuery: { data: null, error: null },
    accountBindingMutation: {
      isPending: false,
      error: null,
      mutateAsync: vi.fn(),
    },
    testEndpointMutation: {
      isPending: false,
      error: null,
      mutateAsync: vi.fn(),
    },
  })),
}));

const { useAccounts } = await import("@/features/accounts/hooks/use-accounts");
const mockedUseAccounts = useAccounts as unknown as ReturnType<typeof vi.fn>;
const { useUpstreamProxyAdmin } = await import("@/features/settings/hooks/use-settings");
const mockedUseUpstreamProxyAdmin = useUpstreamProxyAdmin as unknown as ReturnType<typeof vi.fn>;

function mockAccountsQuery(accounts: AccountSummary[]) {
  mockedUseAccounts.mockReturnValue({
    accountsQuery: { data: accounts, error: null, refetch: vi.fn() },
    importMutation: idleMutation(),
    pauseMutation: idleMutation(),
    resumeMutation: idleMutation(),
    probeMutation: idleMutation(),
    usageResetMutation: idleMutation(),
    deleteMutation: idleMutation(),
    exportAuthMutation: idleMutation(),
    setAliasMutation: idleMutation(),
    limitWarmupMutation: idleMutation(),
    routingPolicyMutation: idleMutation(),
    updateMutation: idleMutation(),
  } as unknown as ReturnType<typeof useAccounts>);
}

function idleMutation() {
  return {
    isPending: false,
    error: null,
    mutateAsync: vi.fn(),
  };
}

function account(overrides: Partial<AccountSummary>): AccountSummary {
  return {
    accountId: "acc-default",
    email: "default@example.com",
    displayName: "Default",
    planType: "plus",
    status: "active",
    additionalQuotas: [],
    limitWarmupEnabled: false,
    ...overrides,
  };
}

describe("AccountsPage", () => {
  beforeEach(() => {
    // The auth store starts least-privilege; these cases exercise admin actions.
    useAuthStore.setState({
      initialized: true,
      authenticated: true,
      role: "admin",
      permissions: ADMIN_PERMISSIONS,
      canWrite: true,
    });
    useAccountQuotaDisplayStore.setState({ quotaDisplay: "weekly" });
    vi.spyOn(Date, "now").mockReturnValue(
      new Date("2026-01-01T12:00:00.000Z").getTime(),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    useAuthStore.setState({ role: "admin", permissions: ADMIN_PERMISSIONS, canWrite: true });
  });

  it("filters provider families without retaining another family's selected detail", async () => {
    const user = userEvent.setup();
    mockAccountsQuery([account({ alias: "Local Codex label", displayName: "Local Codex label" })]);
    server.use(http.get("/api/model-sources/", () => HttpResponse.json({ sources: [
      createModelSource({ id: "claude-team", name: "Claude subscription", provider: "claude", authType: "claude_oauth", accountLabel: "team@example.com" }),
      createModelSource({ id: "anthropic-key", name: "Claude API without email", provider: "anthropic", authType: "api_key" }),
      createModelSource({ id: "openai-key", name: "API named Claude backup", provider: "openai", authType: "api_key" }),
      createModelSource({ id: "deepseek-key", name: "DeepSeek API", provider: "deepseek", authType: "api_key" }),
    ] })));
    render(<MemoryRouter initialEntries={["/accounts?selected=source%3Aclaude-team"]}><AccountsPage /></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "team@example.com" })).toBeInTheDocument();
    const list = within(screen.getByTestId("account-list-scroll-region"));
    await user.click(screen.getByRole("button", { name: /^Codex \d+$/ }));
    expect(screen.getByRole("heading", { name: "default@example.com" })).toBeInTheDocument();
    expect(list.getByRole("button", { name: /^default@example.com/ })).toHaveAttribute("aria-pressed", "true");
    expect(list.getByRole("button", { name: /^API named Claude backup/ })).toBeInTheDocument();
    expect(list.queryByRole("button", { name: /^team@example.com/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Claude 2$/ }));
    expect(screen.getByRole("heading", { name: "team@example.com" })).toBeInTheDocument();
    expect(list.getByRole("button", { name: /^Claude API without email/ })).toBeInTheDocument();
    expect(list.queryByRole("button", { name: /^API named Claude backup/ })).not.toBeInTheDocument();
    expect(list.queryByRole("button", { name: /^default@example.com/ })).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("Search accounts..."), "team@example.com");
    expect(list.queryByRole("button", { name: /^Claude API without email/ })).not.toBeInTheDocument();
    await user.clear(screen.getByPlaceholderText("Search accounts..."));
    await user.click(screen.getByRole("button", { name: /^Other API 1$/ }));
    expect(screen.getByRole("heading", { name: "DeepSeek API" })).toBeInTheDocument();
    expect(list.queryByRole("button", { name: /^team@example.com/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^All \d+$/ }));
    expect(list.getByRole("button", { name: /^default@example.com/ })).toBeInTheDocument();
    expect(list.getByRole("button", { name: /^team@example.com/ })).toBeInTheDocument();
  });

  it("shows an empty category instead of a hidden account's detail", async () => {
    const user = userEvent.setup();
    mockAccountsQuery([account({})]);
    server.use(http.get("/api/model-sources/", () => HttpResponse.json({ sources: [] })));
    render(<MemoryRouter><AccountsPage /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: /^Claude 0$/ }));
    expect(screen.getByText("No matching accounts")).toBeInTheDocument();
    expect(screen.getByText("Select an account")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "default@example.com" })).not.toBeInTheDocument();
  });

  it("exposes source read failures and allows retry without leaking upstream error text", async () => {
    const user = userEvent.setup();
    mockAccountsQuery([account({})]);
    server.use(http.get("/api/model-sources/", () => HttpResponse.json({ error: { code: "provider_failed", message: "Bearer private-secret" } }, { status: 502 })));
    render(<MemoryRouter><AccountsPage /></MemoryRouter>);
    expect(await screen.findByText(/Source accounts could not be loaded/)).toBeInTheDocument();
    expect(screen.queryByText(/private-secret/)).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "default@example.com" })).toBeInTheDocument();
    server.use(http.get("/api/model-sources/", () => HttpResponse.json({ sources: [createModelSource({ name: "Recovered source" })] })));
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: /^Recovered source/ })).toBeInTheDocument();
    expect(screen.queryByText(/Source accounts could not be loaded/)).not.toBeInTheDocument();
  });

  it("treats a missing source list as a read failure, not an empty account collection", async () => {
    mockAccountsQuery([]);
    server.use(http.get("/api/model-sources/", () => HttpResponse.json({})));
    render(<MemoryRouter><AccountsPage /></MemoryRouter>);
    expect(await screen.findByText(/Source accounts could not be loaded/)).toBeInTheDocument();
  });

  it("hides cached source identities when the backend revokes source read access", async () => {
    const user = userEvent.setup();
    mockAccountsQuery([account({})]);
    server.use(http.get("/api/model-sources/", () => HttpResponse.json({ sources: [createModelSource({ name: "Private source" })] })));
    const { queryClient } = renderWithProviders(<AccountsPage />);
    expect(await screen.findByRole("button", { name: /^Private source/ })).toBeInTheDocument();
    server.use(http.get("/api/model-sources/", () => HttpResponse.json({ error: { code: "forbidden", message: "Denied" } }, { status: 403 })));
    void queryClient.invalidateQueries({ queryKey: ["model-sources", "list"] });
    expect(await screen.findByText(/Source accounts could not be loaded/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Private source/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(screen.queryByRole("button", { name: /^Private source/ })).not.toBeInTheDocument();
  });

  it("keeps the upstream-proxy admin query idle and hides OAuth help for read-only guests", () => {
    useAuthStore.setState({ role: "guest", permissions: ["read"], canWrite: false, initialized: true });
    // Guests receive a masked identity: no ChatGPT account/workspace ids and a redacted email.
    mockAccountsQuery([
      account({
        accountId: "acc-masked",
        email: "m***@example.com",
        displayName: "m***@example.com",
        chatgptAccountId: null,
        workspaceId: null,
      }),
    ]);

    render(
      <MemoryRouter>
        <AccountsPage />
      </MemoryRouter>,
    );

    expect(mockedUseUpstreamProxyAdmin).toHaveBeenCalledWith({ enabled: false });
    expect(screen.queryByRole("button", { name: "Need help?" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add account" })).toBeDisabled();
    expect(screen.getByRole("heading", { name: "m***@example.com" })).toBeInTheDocument();
    expect(screen.getAllByText(/Personal \/ unknown workspace/).length).toBeGreaterThan(0);
  });

  it("does not render cached upstream-proxy data in the proxy-binding panel for read-only guests", () => {
    useAuthStore.setState({ role: "guest", permissions: ["read"], canWrite: false, initialized: true });
    // `enabled: false` only stops fetching; an earlier admin session's response
    // can still be in the cache and must not reach the panel.
    mockedUseUpstreamProxyAdmin.mockReturnValue({
      upstreamProxyQuery: { data: createUpstreamProxyAdmin(), error: null },
      accountBindingMutation: { isPending: false, error: null, mutateAsync: vi.fn() },
      testEndpointMutation: { isPending: false, error: null, mutateAsync: vi.fn() },
    });
    mockAccountsQuery([account({ accountId: "acc_primary" })]);

    render(
      <MemoryRouter>
        <AccountsPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "Accounts" })).toBeInTheDocument();
    expect(screen.queryByText("Proxy binding")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Account proxy pool" })).not.toBeInTheDocument();
    expect(screen.queryByText("Primary pool")).not.toBeInTheDocument();
  });

  it("renders the proxy-binding panel from upstream-proxy data for writers", () => {
    mockedUseUpstreamProxyAdmin.mockReturnValue({
      upstreamProxyQuery: { data: createUpstreamProxyAdmin(), error: null },
      accountBindingMutation: { isPending: false, error: null, mutateAsync: vi.fn() },
      testEndpointMutation: { isPending: false, error: null, mutateAsync: vi.fn() },
    });
    mockAccountsQuery([account({ accountId: "acc_primary" })]);

    render(
      <MemoryRouter>
        <AccountsPage />
      </MemoryRouter>,
    );

    expect(screen.getByText("Proxy binding")).toBeInTheDocument();
  });

  it("enables the upstream-proxy admin query and shows OAuth help for writers", () => {
    mockAccountsQuery([account({})]);

    render(
      <MemoryRouter>
        <AccountsPage />
      </MemoryRouter>,
    );

    expect(mockedUseUpstreamProxyAdmin).toHaveBeenCalledWith({ enabled: true });
    expect(screen.getByRole("button", { name: "Need help?" })).toBeInTheDocument();
  });

  it("does not offer source creation to account writers without the source write grant", async () => {
    const user = userEvent.setup();
    useAuthStore.setState({ permissions: ["accounts:read:all", "accounts:write:all"], canWrite: false });
    mockAccountsQuery([]);
    render(
      <MemoryRouter>
        <AccountsPage />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Add account" }));

    expect(screen.getByRole("button", { name: /^Codex subscription/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Import/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^OpenAI/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Claude/ })).not.toBeInTheDocument();
  });

  it("creates a custom source and selects its monitoring in the Accounts right panel", async () => {
    const user = userEvent.setup();
    mockAccountsQuery([]);
    const { queryClient } = renderWithProviders(<AccountsPage />);

    await user.click(screen.getByRole("button", { name: "Add account" }));
    await user.click(screen.getByRole("button", { name: /^Other providers/ }));
    const dialog = await screen.findByRole("dialog", { name: "Create model source" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "Accounts test source");
    await user.type(within(dialog).getByRole("textbox", { name: "Base URL" }), "https://upstream.example/v1");
    await user.type(within(dialog).getByLabelText("Upstream API key"), "synthetic-upstream-key");
    await user.type(within(dialog).getByRole("textbox", { name: "Models" }), "accounts-test-model");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Create model source" })).not.toBeInTheDocument());
    expect(await screen.findByRole("heading", { name: "Accounts test source" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Accounts test source/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("region", { name: "Observed gateway usage · last 24 hours" })).toBeInTheDocument();
    expect(window.location.search).toContain("selected=source%3A");
    expect(JSON.stringify(queryClient.getMutationCache().getAll().map((mutation) => mutation.state.variables))).not.toContain("synthetic-upstream-key");

  });

  it.each([
    ["OpenAI", "openai", "OpenAI / ChatGPT API"],
    ["Claude API", "anthropic", "Claude API"],
  ])("registers %s directly with an API key and immediately selects its monitoring", async (choice, provider, name) => {
    const user = userEvent.setup();
    mockAccountsQuery([]);
    const { queryClient } = renderWithProviders(<AccountsPage />);
    await user.click(screen.getByRole("button", { name: "Add account" }));
    await user.click(screen.getByRole("button", { name: new RegExp(`^${choice}`) }));
    const dialog = await screen.findByRole("dialog", { name: "Create model source" });
    const key = await within(dialog).findByLabelText("Upstream API key");
    await waitFor(() => expect(key).toBeEnabled());
    expect(within(dialog).queryByRole("textbox", { name: "Base URL" })).not.toBeInTheDocument();
    await user.type(key, "synthetic-direct-provider-key");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(await screen.findByRole("heading", { name })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: new RegExp(`^${name}`) })).toHaveAttribute("aria-pressed", "true");
    expect(queryClient.getQueryData<{ sources: Array<{ provider: string; authType: string; billingMode: string }> }>(["model-sources", "list"])?.sources).toEqual(expect.arrayContaining([expect.objectContaining({ provider, authType: "api_key", billingMode: "api_metered" })]));
    expect(JSON.stringify(queryClient.getMutationCache().getAll().map((mutation) => mutation.state.variables))).not.toContain("synthetic-direct-provider-key");
  });

  it("selects an existing exhausted Claude source from the query for read-only monitoring", async () => {
    const source = createModelSource({ id: "existing-claude", name: "Live Claude", provider: "claude", authType: "claude_oauth", billingMode: "subscription", entitlementStatus: "verified", quotaWindows: [{ windowKey: "seven_day", model: null, usedRatio: 1, usedValue: null, limitValue: null, resetsAt: null, observedAt: "2026-01-01T11:59:00Z" }] });
    server.use(http.get("/api/model-sources/", () => HttpResponse.json({ sources: [source] })));
    useAuthStore.setState({ permissions: ["read", "accounts:read:all"], canWrite: false });
    mockAccountsQuery([account({ accountId: "codex", displayName: "Existing Codex" })]);
    render(<MemoryRouter initialEntries={["/accounts?selected=source%3Aexisting-claude"]}><AccountsPage /></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "Live Claude" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Live Claude/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Add account" })).toBeDisabled();
    for (const action of screen.queryAllByRole("button", { name: /Connect \/ import|Reauthorize|Refresh entitlement/i })) expect(action).toBeDisabled();
    expect(screen.getByText("Existing Codex")).toBeInTheDocument();
  });

  it("creates a Claude subscription then hands off directly to OAuth or credential import", async () => {
    const user = userEvent.setup();
    mockAccountsQuery([]);
    renderWithProviders(<AccountsPage />);
    await user.click(screen.getByRole("button", { name: "Add account" }));
    await user.click(screen.getByRole("button", { name: /^Claude subscription/ }));
    const create = await screen.findByRole("dialog", { name: "Create model source" });
    await waitFor(() => expect(within(create).getByRole("textbox", { name: "Name" })).toHaveValue("Claude subscription"));
    expect(within(create).queryByLabelText("Upstream API key")).not.toBeInTheDocument();
    await user.click(within(create).getByRole("button", { name: "Create" }));
    const connect = await screen.findByRole("dialog", { name: "Connect Claude subscription" });
    expect(within(connect).getByRole("button", { name: "Start Claude OAuth" })).toBeInTheDocument();
    expect(within(connect).getByLabelText("Credential JSON file")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Claude subscription", hidden: true })).toBeInTheDocument();
  });

  it("defaults the selected account to the first account after display sorting", () => {
    mockedUseAccounts.mockReturnValue({
      accountsQuery: {
        data: [
          account({
            accountId: "acc-api-first",
            email: "api-first@example.com",
            displayName: "API First",
            resetAtSecondary: "2026-01-01T13:00:00.000Z",
            windowMinutesSecondary: 10_080,
          }),
          account({
            accountId: "acc-visible-first",
            email: "visible-first@example.com",
            displayName: "Visible First",
            resetAtSecondary: "2026-01-01T12:10:00.000Z",
            windowMinutesSecondary: 10_080,
          }),
        ],
        error: null,
        refetch: vi.fn(),
      },
      importMutation: idleMutation(),
      pauseMutation: idleMutation(),
      resumeMutation: idleMutation(),
      probeMutation: idleMutation(),
      usageResetMutation: idleMutation(),
      deleteMutation: idleMutation(),
      exportAuthMutation: idleMutation(),
      setAliasMutation: idleMutation(),
      limitWarmupMutation: idleMutation(),
      routingPolicyMutation: idleMutation(),
      updateMutation: idleMutation(),
    } as unknown as ReturnType<typeof useAccounts>);

    render(
      <MemoryRouter>
        <AccountsPage />
      </MemoryRouter>,
    );

    expect(
      screen
        .getAllByText(/^(Visible First|API First)$/)
        .map((el) => el.textContent),
    ).toEqual(["Visible First", "API First", "Visible First"]);
    expect(
      screen.getByRole("heading", { name: "visible-first@example.com" }),
    ).toBeInTheDocument();
  });


  it("keeps helper instructions accessible when no accounts exist", async () => {
    const user = userEvent.setup();

    mockedUseAccounts.mockReturnValue({
      accountsQuery: {
        data: [],
        error: null,
        refetch: vi.fn(),
      },
      importMutation: idleMutation(),
      pauseMutation: idleMutation(),
      resumeMutation: idleMutation(),
      probeMutation: idleMutation(),
      usageResetMutation: idleMutation(),
      deleteMutation: idleMutation(),
      exportAuthMutation: idleMutation(),
      setAliasMutation: idleMutation(),
      limitWarmupMutation: idleMutation(),
      routingPolicyMutation: idleMutation(),
      updateMutation: idleMutation(),
    } as unknown as ReturnType<typeof useAccounts>);

    render(
      <MemoryRouter>
        <AccountsPage />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Need help?" }));
    expect(screen.getByText("Windows OAuth Help")).toBeInTheDocument();
  });

  it("confirms before resetting selected account usage", async () => {
    const user = userEvent.setup();
    const resetUsage = vi.fn().mockResolvedValue({
      status: "reset",
      accountId: "acc-reset",
      code: "reset",
      windowsReset: 2,
      usageWritten: true,
      primaryUsedPercentBefore: 100,
      primaryUsedPercentAfter: 2,
      secondaryUsedPercentBefore: 80,
      secondaryUsedPercentAfter: 0,
      accountStatusBefore: "rate_limited",
      accountStatusAfter: "active",
    });

    mockedUseAccounts.mockReturnValue({
      accountsQuery: {
        data: [
          account({
            accountId: "acc-reset",
            email: "reset@example.com",
            displayName: "Resettable",
            resetAtSecondary: "2026-01-01T13:00:00.000Z",
            windowMinutesSecondary: 10_080,
          }),
        ],
        error: null,
        refetch: vi.fn(),
      },
      importMutation: idleMutation(),
      pauseMutation: idleMutation(),
      resumeMutation: idleMutation(),
      probeMutation: idleMutation(),
      usageResetMutation: {
        isPending: false,
        error: null,
        mutateAsync: resetUsage,
      },
      deleteMutation: idleMutation(),
      exportAuthMutation: idleMutation(),
      setAliasMutation: idleMutation(),
      limitWarmupMutation: idleMutation(),
      routingPolicyMutation: idleMutation(),
      updateMutation: idleMutation(),
    } as unknown as ReturnType<typeof useAccounts>);

    render(
      <MemoryRouter>
        <AccountsPage />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Reset usage" }));

    const dialog = await screen.findByRole("alertdialog", { name: "Reset usage" });
    expect(resetUsage).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Reset" }));

    await waitFor(() => {
      expect(resetUsage).toHaveBeenCalledWith({ accountId: "acc-reset" });
    });
  });

  it("keeps force probe as an immediate action", async () => {
    const user = userEvent.setup();
    const probe = vi.fn().mockResolvedValue({
      status: "probed",
      accountId: "acc-probe",
      probeStatusCode: 200,
      primaryUsedPercentBefore: 10,
      primaryUsedPercentAfter: 9,
      secondaryUsedPercentBefore: 20,
      secondaryUsedPercentAfter: 19,
      accountStatusBefore: "active",
      accountStatusAfter: "active",
    });

    mockedUseAccounts.mockReturnValue({
      accountsQuery: {
        data: [
          account({
            accountId: "acc-probe",
            email: "probe@example.com",
            displayName: "Probe account",
            resetAtSecondary: "2026-01-01T13:00:00.000Z",
            windowMinutesSecondary: 10_080,
          }),
        ],
        error: null,
        refetch: vi.fn(),
      },
      importMutation: idleMutation(),
      pauseMutation: idleMutation(),
      resumeMutation: idleMutation(),
      probeMutation: {
        isPending: false,
        error: null,
        mutateAsync: probe,
      },
      usageResetMutation: idleMutation(),
      deleteMutation: idleMutation(),
      exportAuthMutation: idleMutation(),
      setAliasMutation: idleMutation(),
      limitWarmupMutation: idleMutation(),
      routingPolicyMutation: idleMutation(),
      updateMutation: idleMutation(),
    } as unknown as ReturnType<typeof useAccounts>);

    render(
      <MemoryRouter>
        <AccountsPage />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Force probe" }));

    expect(probe).toHaveBeenCalledWith({ accountId: "acc-probe" });
    expect(screen.queryByRole("alertdialog", { name: "Reset usage" })).not.toBeInTheDocument();
  });
});
