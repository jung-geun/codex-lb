import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AccountList } from "@/features/accounts/components/account-list";
import { useAccountQuotaDisplayStore } from "@/hooks/use-account-quota-display";
import { createAccountSummary, createModelSource } from "@/test/mocks/factories";

describe("AccountList", () => {
  beforeEach(() => {
    useAccountQuotaDisplayStore.setState({ quotaDisplay: "both" });
    vi.spyOn(Date, "now").mockReturnValue(
      new Date("2026-01-01T12:00:00.000Z").getTime(),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each([
    ["openai", "Claude API backup", "Codex"],
    ["claude", "OpenAI API backup", "Claude"],
    ["anthropic", "Codex subscription", "Claude"],
    ["custom", "Claude Codex subscription", "Other API"],
    ["zai", "Claude Codex subscription", "Other API"],
    ["kimi", "Claude Codex subscription", "Other API"],
    ["nanogpt", "Claude Codex subscription", "Other API"],
    ["deepseek", "Claude Codex subscription", "Other API"],
    [undefined, "Claude Codex subscription", "Other API"],
  ] as const)("groups provider %s by provider rather than its name", async (provider, name, category) => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<AccountList
      accounts={[createAccountSummary({ email: "codex@example.com" })]}
      sources={[createModelSource({ id: "misleading-source", provider, name, accountLabel: "source@example.com", authType: "api_key", billingMode: "api_metered" })]}
      selectedAccountId="source:misleading-source"
      onSelect={onSelect}
      onOpenImport={vi.fn()}
      onOpenOauth={vi.fn()}
      readOnly
    />);

    const group = screen.getByRole("region", { name: category });
    const source = within(group).getByRole("button", { name: /source@example.com/ });
    expect(source).toHaveAttribute("aria-pressed", "true");
    expect(source).toHaveTextContent(name);
    expect(within(group).getByRole("heading", { name: `${category} ${category === "Codex" ? 2 : 1}` })).toBeInTheDocument();
    await user.click(source);
    expect(onSelect).toHaveBeenCalledWith("source:misleading-source");

    for (const filter of ["Codex", "Claude", "Other API"]) {
      const count = filter === "Codex" ? (category === "Codex" ? 2 : 1) : filter === category ? 1 : 0;
      await user.click(screen.getByRole("button", { name: `${filter} ${count}` }));
      expect(screen.queryByRole("button", { name: /source@example.com/ }) !== null).toBe(filter === category);
      expect(screen.queryByText("codex@example.com") !== null).toBe(filter === "Codex");
    }
  });

  it.each([
    ["claude", "claude_oauth", 1, "2026-01-01T11:50:00Z", "2026-01-01T13:00:00Z"],
    ["claude", "claude_oauth", 0.2, "2026-01-01T11:50:00Z", "2026-01-01T13:00:00Z"],
    ["openai", "api_key", 1, "2026-01-01T11:50:00Z", "2026-01-01T13:00:00Z"],
    ["openai", "api_key", 0.2, "2026-01-01T11:50:00Z", "2026-01-01T13:00:00Z"],
    ["claude", "claude_oauth", 1, "2026-01-01T11:59:00Z", "2026-01-01T12:00:00Z"],
    ["claude", "claude_oauth", 0.2, "2026-01-01T11:59:00Z", "2026-01-01T12:00:00Z"],
    ["openai", "api_key", 1, "2026-01-01T11:59:00Z", "2026-01-01T12:00:00Z"],
    ["openai", "api_key", 0.2, "2026-01-01T11:59:00Z", "2026-01-01T12:00:00Z"],
  ] as const)("does not filter stale %s %s ratio %s observed %s resetting %s as active or exhausted", async (provider, authType, usedRatio, observedAt, resetsAt) => {
    const user = userEvent.setup();
    render(<AccountList
      accounts={[]}
      sources={[createModelSource({ id: "stale-source", name: "Old quota observation", provider, authType, healthStatus: "healthy", entitlementStatus: "verified", quotaWindows: [{ windowKey: "five_hour", model: null, usedRatio, usedValue: null, limitValue: null, observedAt, resetsAt }] })]}
      selectedAccountId={null}
      onSelect={vi.fn()}
      onOpenImport={vi.fn()}
      onOpenOauth={vi.fn()}
    />);

    for (const [label, visible] of [[/^Active$/, false], [/^Quota exceeded$/, false], [/^Stale/, true]] as const) {
      await user.click(screen.getByRole("combobox", { name: "Filter accounts by status" }));
      await user.click(screen.getByRole("option", { name: label }));
      expect(screen.queryByRole("button", { name: /Old quota observation/ }) !== null).toBe(visible);
    }
  });

  it.each([true, false])("shows API fallback policy %s in the account row", (apiFallbackOnly) => {
    render(<AccountList
      accounts={[]}
      sources={[createModelSource({ provider: "openai", billingMode: "api_metered", apiFallbackOnly })]}
      selectedAccountId={null}
      onSelect={vi.fn()}
      onOpenImport={vi.fn()}
      onOpenOauth={vi.fn()}
      readOnly
    />);
    expect(screen.getByText(apiFallbackOnly ? "API only after subscriptions" : "API participates alongside subscriptions")).toBeInTheDocument();
  });

  it.each([
    ["Codex subscription", "oauth", undefined],
    ["Import", "import", undefined],
    ["OpenAI", "source", "openai"],
    ["Claude API", "source", "anthropic"],
    ["Claude subscription", "source", "claude"],
    ["Other providers", "source", "custom"],
  ] as const)("opens the %s registration path after closing the chooser", async (label, action, provider) => {
    const user = userEvent.setup();
    const actions = { oauth: vi.fn(), import: vi.fn(), source: vi.fn() };
    render(
      <AccountList
        accounts={[]}
        selectedAccountId={null}
        onSelect={vi.fn()}
        onOpenImport={actions.import}
        onOpenOauth={actions.oauth}
        onOpenSource={actions.source}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add account" }));
    expect(screen.getByRole("button", { name: /^Codex subscription/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Import/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: new RegExp(`^${label}`) }));

    await waitFor(() => expect(actions[action]).toHaveBeenCalledOnce());
    if (provider) expect(actions.source).toHaveBeenCalledWith(provider);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    for (const [name, callback] of Object.entries(actions)) {
      if (name !== action) expect(callback).not.toHaveBeenCalled();
    }
  });

  it("closes the chooser when the account list becomes read-only", async () => {
    const user = userEvent.setup();
    const props = {
      accounts: [],
      selectedAccountId: null,
      onSelect: vi.fn(),
      onOpenImport: vi.fn(),
      onOpenOauth: vi.fn(),
      onOpenSource: vi.fn(),
    };
    const { rerender } = render(<AccountList {...props} />);
    await user.click(screen.getByRole("button", { name: "Add account" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    rerender(<AccountList {...props} readOnly />);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add account" })).toBeDisabled();
    expect(props.onOpenSource).not.toHaveBeenCalled();
  });

  it("keeps existing sources selectable and searchable alongside Codex for read-only users", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<AccountList
      accounts={[{ accountId: "codex", email: "codex@example.com", displayName: "Codex", planType: "plus", status: "active", limitWarmupEnabled: false, additionalQuotas: [] }]}
      sources={[createModelSource({ id: "live-claude", name: "Existing Claude", provider: "claude", authType: "claude_oauth", billingMode: "subscription", quotaWindows: [{ windowKey: "seven_day", model: null, usedRatio: 1, usedValue: null, limitValue: null, observedAt: "2026-01-01T11:59:00Z", resetsAt: null }] })]}
      selectedAccountId="source:live-claude"
      onSelect={onSelect}
      onOpenImport={vi.fn()}
      onOpenOauth={vi.fn()}
      readOnly
    />);
    expect(screen.getByText("codex@example.com")).toBeInTheDocument();
    const source = screen.getByRole("button", { name: /Existing Claude/ });
    expect(source).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Add account" })).toBeDisabled();
    await user.click(source);
    expect(onSelect).toHaveBeenCalledWith("source:live-claude");
    await user.type(screen.getByPlaceholderText("Search accounts..."), "claude");
    expect(screen.queryByText("codex@example.com")).not.toBeInTheDocument();
    expect(screen.getByText("Existing Claude")).toBeInTheDocument();
    await user.clear(screen.getByPlaceholderText("Search accounts..."));
    await user.click(screen.getByRole("combobox", { name: "Filter accounts by status" }));
    await user.click(screen.getByRole("option", { name: "Quota exceeded" }));
    expect(screen.getByText("Existing Claude")).toBeInTheDocument();
    expect(screen.queryByText("codex@example.com")).not.toBeInTheDocument();
  });

  it("renders items and filters by search", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();

    render(
      <AccountList
        accounts={[
          {
            accountId: "acc-1",
            email: "primary@example.com",
            displayName: "Primary",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            additionalQuotas: [],
          },
          {
            accountId: "acc-2",
            email: "secondary@example.com",
            displayName: "Secondary",
            planType: "pro",
            status: "paused",
            limitWarmupEnabled: false,
            additionalQuotas: [],
          },
        ]}
        selectedAccountId="acc-1"
        onSelect={onSelect}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
      />,
    );

    expect(screen.getByText("primary@example.com")).toBeInTheDocument();
    expect(screen.getByText("secondary@example.com")).toBeInTheDocument();

    await user.type(
      screen.getByPlaceholderText("Search accounts..."),
      "secondary",
    );
    expect(screen.queryByText("primary@example.com")).not.toBeInTheDocument();
    expect(screen.getByText("secondary@example.com")).toBeInTheDocument();

    await user.click(screen.getByText("secondary@example.com"));
    expect(onSelect).toHaveBeenCalledWith("acc-2");
  });

  it("sorts accounts by the rows actually rendered", () => {
    useAccountQuotaDisplayStore.setState({ quotaDisplay: "weekly" });

    render(
      <AccountList
        accounts={[
          {
            accountId: "acc-hidden-early",
            email: "hidden-early@example.com",
            displayName: "Hidden Early",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            usage: {
              primaryRemainingPercent: 42,
              secondaryRemainingPercent: 18,
            },
            resetAtPrimary: "2026-01-01T12:05:00.000Z",
            resetAtSecondary: "2026-01-01T13:00:00.000Z",
            windowMinutesPrimary: 300,
            windowMinutesSecondary: 10_080,
            additionalQuotas: [],
          },
          {
            accountId: "acc-visible-early",
            email: "visible-early@example.com",
            displayName: "Visible Early",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            usage: {
              primaryRemainingPercent: 82,
              secondaryRemainingPercent: 73,
            },
            resetAtPrimary: "2026-01-01T12:30:00.000Z",
            resetAtSecondary: "2026-01-01T12:10:00.000Z",
            windowMinutesPrimary: 300,
            windowMinutesSecondary: 10_080,
            additionalQuotas: [],
          },
        ]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
      />,
    );

    expect(
      screen
        .getAllByText(/^(Hidden Early|Visible Early)$/)
        .map((el) => el.textContent),
    ).toEqual(["Visible Early", "Hidden Early"]);
  });

  it("ignores elapsed reset timestamps when sorting", () => {
    render(
      <AccountList
        accounts={[
          {
            accountId: "acc-stale",
            email: "stale@example.com",
            displayName: "Stale",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            usage: {
              primaryRemainingPercent: 42,
              secondaryRemainingPercent: 18,
            },
            resetAtPrimary: "2026-01-01T11:30:00.000Z",
            resetAtSecondary: "2026-01-01T11:45:00.000Z",
            windowMinutesPrimary: 300,
            windowMinutesSecondary: 10_080,
            additionalQuotas: [],
          },
          {
            accountId: "acc-fresh",
            email: "fresh@example.com",
            displayName: "Fresh",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            usage: {
              primaryRemainingPercent: 82,
              secondaryRemainingPercent: 73,
            },
            resetAtPrimary: "2026-01-01T12:30:00.000Z",
            resetAtSecondary: "2026-01-01T12:20:00.000Z",
            windowMinutesPrimary: 300,
            windowMinutesSecondary: 10_080,
            additionalQuotas: [],
          },
        ]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
      />,
    );

    expect(
      screen.getAllByText(/^(Fresh|Stale)$/).map((el) => el.textContent),
    ).toEqual(["Fresh", "Stale"]);
  });

  it("sorts legacy primary quota rows by their reset timestamp", () => {
    render(
      <AccountList
        accounts={[
          {
            accountId: "acc-late",
            email: "late@example.com",
            displayName: "Late",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            usage: {
              primaryRemainingPercent: 42,
              secondaryRemainingPercent: null,
            },
            resetAtPrimary: "2026-01-01T13:00:00.000Z",
            resetAtSecondary: null,
            windowMinutesPrimary: null,
            windowMinutesSecondary: null,
            additionalQuotas: [],
          },
          {
            accountId: "acc-early",
            email: "early@example.com",
            displayName: "Early",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            usage: {
              primaryRemainingPercent: 82,
              secondaryRemainingPercent: null,
            },
            resetAtPrimary: "2026-01-01T12:10:00.000Z",
            resetAtSecondary: null,
            windowMinutesPrimary: null,
            windowMinutesSecondary: null,
            additionalQuotas: [],
          },
        ]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
      />,
    );

    expect(
      screen.getAllByText(/^(Early|Late)$/).map((el) => el.textContent),
    ).toEqual(["Early", "Late"]);
  });

  it("sorts accounts by name", () => {
    render(
      <AccountList
        accounts={[
          {
            accountId: "acc-z",
            email: "z@example.com",
            displayName: "Zeta",
            planType: "pro",
            status: "active",
            limitWarmupEnabled: false,
            resetAtPrimary: "2026-01-01T12:30:00.000Z",
            additionalQuotas: [],
          },
          {
            accountId: "acc-a",
            email: "a@example.com",
            displayName: "Alpha",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            resetAtPrimary: "2026-01-01T12:10:00.000Z",
            additionalQuotas: [],
          },
        ]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
        sortMode="name_asc"
        onSortModeChange={() => {}}
      />,
    );

    expect(screen.getAllByText(/^(Alpha|Zeta)$/).map((el) => el.textContent)).toEqual([
      "Alpha",
      "Zeta",
    ]);
  });

  it("supports reverse name sorting", () => {
    render(
      <AccountList
        accounts={[
          {
            accountId: "acc-b",
            email: "b@example.com",
            displayName: "Beta",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            resetAtPrimary: "2026-01-01T12:10:00.000Z",
            additionalQuotas: [],
          },
          {
            accountId: "acc-a",
            email: "a@example.com",
            displayName: "Alpha",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            resetAtPrimary: "2026-01-01T12:20:00.000Z",
            additionalQuotas: [],
          },
        ]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
        sortMode="name_desc"
        onSortModeChange={() => {}}
      />,
    );

    expect(screen.getAllByText(/^(Alpha|Beta)$/).map((el) => el.textContent)).toEqual([
      "Beta",
      "Alpha",
    ]);
  });

  it("can sort by latest reset first", () => {
    render(
      <AccountList
        accounts={[
          {
            accountId: "acc-a",
            email: "a@example.com",
            displayName: "Alpha",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            resetAtPrimary: "2026-01-01T12:10:00.000Z",
            additionalQuotas: [],
          },
          {
            accountId: "acc-z",
            email: "z@example.com",
            displayName: "Zeta",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            resetAtPrimary: "2026-01-01T12:40:00.000Z",
            additionalQuotas: [],
          },
        ]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
        sortMode="reset_latest"
        onSortModeChange={() => {}}
      />,
    );

    expect(screen.getAllByText(/^(Zeta|Alpha)$/).map((el) => el.textContent)).toEqual([
      "Zeta",
      "Alpha",
    ]);
  });

  it("keeps unknown resets last when sorting by latest reset", () => {
    render(
      <AccountList
        accounts={[
          {
            accountId: "acc-unknown",
            email: "unknown@example.com",
            displayName: "Unknown",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            additionalQuotas: [],
          },
          {
            accountId: "acc-stale",
            email: "stale@example.com",
            displayName: "Stale",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            resetAtPrimary: "2026-01-01T11:30:00.000Z",
            additionalQuotas: [],
          },
          {
            accountId: "acc-latest",
            email: "latest@example.com",
            displayName: "Latest",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            resetAtPrimary: "2026-01-01T12:40:00.000Z",
            additionalQuotas: [],
          },
          {
            accountId: "acc-earlier",
            email: "earlier@example.com",
            displayName: "Earlier",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            resetAtPrimary: "2026-01-01T12:10:00.000Z",
            additionalQuotas: [],
          },
        ]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
        sortMode="reset_latest"
        onSortModeChange={() => {}}
      />,
    );

    expect(screen.getAllByText(/^(Latest|Earlier|Stale|Unknown)$/).map((el) => el.textContent)).toEqual([
      "Latest",
      "Earlier",
      "Stale",
      "Unknown",
    ]);
  });

  it("shows empty state when no items match filter", async () => {
    const user = userEvent.setup();

    render(
      <AccountList
        accounts={[
          {
            accountId: "acc-1",
            email: "primary@example.com",
            displayName: "Primary",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            additionalQuotas: [],
          },
        ]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
      />,
    );

    await user.type(
      screen.getByPlaceholderText("Search accounts..."),
      "not-found",
    );
    expect(screen.getByText("No matching accounts")).toBeInTheDocument();
  });

  it("shows first-run empty copy when no accounts exist", () => {
    render(
      <AccountList
        accounts={[]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
      />,
    );

    expect(screen.getByText("No accounts yet")).toBeInTheDocument();
    expect(screen.getByText("Add an account to start routing.")).toBeInTheDocument();
    expect(screen.queryByText("Adjust filters")).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Individual requests can still skip an Active account/i),
    ).not.toBeInTheDocument();
  });

  it("shows a visible status-vs-eligibility note when accounts exist", () => {
    render(
      <AccountList
        accounts={[
          {
            accountId: "acc-1",
            email: "primary@example.com",
            displayName: "Primary",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            additionalQuotas: [],
          },
        ]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
      />,
    );

    expect(
      screen.getByText(/Individual requests can still skip an Active account/i),
    ).toBeInTheDocument();
  });

  it("keeps the add account action outside the scrollable account list", () => {
    render(
      <AccountList
        accounts={[
          {
            accountId: "acc-1",
            email: "primary@example.com",
            displayName: "Primary",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            additionalQuotas: [],
          },
        ]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
      />,
    );

    const addAccountButton = screen.getByRole("button", { name: "Add account" });
    const scrollRegion = screen.getByTestId("account-list-scroll-region");

    expect(scrollRegion).not.toContainElement(addAccountButton);
  });

  it("keeps the account rows inside a viewport-bounded scroll region", () => {
    render(
      <AccountList
        accounts={Array.from({ length: 20 }, (_, index) => ({
          accountId: `acc-${index}`,
          email: `account-${index}@example.com`,
          displayName: `Account ${index}`,
          planType: "plus",
          status: "active",
          limitWarmupEnabled: false,
          additionalQuotas: [],
        }))}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
      />,
    );

    const scrollRegion = screen.getByTestId("account-list-scroll-region");

    expect(scrollRegion).toHaveClass("overflow-y-auto");
    expect(scrollRegion.parentElement).toHaveClass("max-h-[calc(100dvh-15rem)]");
    expect(scrollRegion).not.toHaveClass("max-h-[calc(100dvh-23rem)]");
    expect(scrollRegion).not.toHaveClass("lg:max-h-none");
  });

  it("filters re-auth required accounts by status", async () => {
    const user = userEvent.setup();

    render(
      <AccountList
        accounts={[
          {
            accountId: "acc-active",
            email: "active@example.com",
            displayName: "Active",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            additionalQuotas: [],
          },
          {
            accountId: "acc-reauth",
            email: "reauth@example.com",
            displayName: "Needs Reauth",
            planType: "pro",
            status: "reauth_required",
            limitWarmupEnabled: false,
            additionalQuotas: [],
          },
        ]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
      />,
    );

    await user.click(screen.getByRole("combobox", { name: "Filter accounts by status" }));
    await user.click(screen.getByRole("option", { name: "Reauth required" }));

    expect(screen.queryByText("active@example.com")).not.toBeInTheDocument();
    expect(screen.getByText("reauth@example.com")).toBeInTheDocument();
  });

  it("uses the backend duplicate indicator instead of recomputing by email", () => {
    render(
      <AccountList
        accounts={[
          {
            accountId: "d48f0bfc-8ea6-48a7-8d76-d0e5ef1816c5_6f12b5d5",
            email: "dup@example.com",
            displayName: "Same email, different workspace",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            isEmailDuplicate: false,
            additionalQuotas: [],
          },
          {
            accountId: "7f9de2ad-7621-4a6f-88bc-ec7f3d914701_91a95cee",
            email: "dup@example.com",
            displayName: "Same email, duplicate slot",
            planType: "plus",
            status: "active",
            limitWarmupEnabled: false,
            isEmailDuplicate: true,
            additionalQuotas: [],
          },
          {
            accountId: "acc-3",
            email: "unique@example.com",
            displayName: "Unique",
            planType: "pro",
            status: "active",
            limitWarmupEnabled: false,
            additionalQuotas: [],
          },
        ]}
        selectedAccountId={null}
        onSelect={() => {}}
        onOpenImport={() => {}}
        onOpenOauth={() => {}}
      />,
    );

    expect(screen.getByRole("button", { name: /^dup@example.com Same email, different workspace/ })).not.toHaveTextContent("ID d48f0bfc");
    expect(screen.getByRole("button", { name: /^dup@example.com Same email, duplicate slot/ })).toHaveTextContent("ID 7f9de2ad...a95cee");
    expect(screen.getByRole("button", { name: /^unique@example.com/ })).not.toHaveTextContent("ID ");
  });
});
