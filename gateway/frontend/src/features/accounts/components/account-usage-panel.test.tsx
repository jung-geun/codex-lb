import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AccountUsagePanel } from "@/features/accounts/components/account-usage-panel";
import { useDateDisplayFormatStore } from "@/hooks/use-date-format";
import { createAccountSummary, createAccountTrends, createModelSource } from "@/test/mocks/factories";
import { formatDateTimeInline } from "@/utils/formatters";
import { sourceToAccountView } from "@/features/accounts/source-selection";
import type { ModelSourceApiUsage } from "@/features/model-sources/schemas";

const apiTelemetry: ModelSourceApiUsage = {
  sourceId: "api", observedAt: "2026-01-01T00:00:00Z",
  rateLimits: [{ model: "gpt-exact", tokenKind: "tokens", observedAt: "2026-01-01T00:00:00Z", tokenLimit: 100000, tokensRemaining: 76543, tokensResetAt: "2026-01-01T00:01:00Z", requestLimit: 500, requestsRemaining: 498, requestsResetAt: "2026-01-01T00:01:00Z" }],
  balances: [], balanceObservedAt: null, balanceStatus: "unavailable", balanceError: null, balanceIsAvailable: null,
};

describe("AccountUsagePanel", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    useDateDisplayFormatStore.setState({ dateDisplayFormat: "iso8601" });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows '--' for missing quota percent instead of 0%", () => {
    const account = createAccountSummary({
      usage: {
        primaryRemainingPercent: null,
        secondaryRemainingPercent: 67,
      },
      windowMinutesPrimary: 300,
      windowMinutesSecondary: 10_080,
    });

    render(<AccountUsagePanel account={account} trends={null} />);

    expect(screen.getByText("5h remaining")).toBeInTheDocument();
    expect(screen.getByText("--")).toBeInTheDocument();
  });

  it("hides 5h row for weekly-only accounts", () => {
    const account = createAccountSummary({
      planType: "free",
      usage: {
        primaryRemainingPercent: null,
        secondaryRemainingPercent: 76,
      },
      windowMinutesPrimary: null,
      windowMinutesSecondary: 10_080,
    });

    render(<AccountUsagePanel account={account} trends={null} />);

    expect(screen.queryByText("5h remaining")).not.toBeInTheDocument();
    expect(screen.getByText("Weekly remaining")).toBeInTheDocument();
  });

  it("shows only Monthly for monthly-only free accounts", () => {
    const account = createAccountSummary({
      planType: "free",
      usage: {
        primaryRemainingPercent: null,
        secondaryRemainingPercent: null,
        monthlyRemainingPercent: 95,
      },
      windowMinutesPrimary: null,
      windowMinutesSecondary: null,
      windowMinutesMonthly: 43_200,
      resetAtPrimary: null,
      resetAtSecondary: null,
      resetAtMonthly: "2026-01-31T00:00:00.000Z",
    });

    render(<AccountUsagePanel account={account} trends={null} />);

    expect(screen.getByText("Monthly remaining")).toBeInTheDocument();
    expect(screen.queryByText("5h remaining")).not.toBeInTheDocument();
    expect(screen.queryByText("Weekly remaining")).not.toBeInTheDocument();
  });

  it("renders mapped label for the known gated additional quota limit", () => {
    const account = createAccountSummary({
      additionalQuotas: [
        {
          limitName: "codex_spark",
          meteredFeature: "codex_bengalfox",
          routingPolicy: "inherit",
          primaryWindow: {
            usedPercent: 35,
            resetAt: Math.floor(new Date("2026-01-07T13:00:00.000Z").getTime() / 1000),
            windowMinutes: 300,
          },
          secondaryWindow: null,
        },
      ],
    });

    render(<AccountUsagePanel account={account} trends={null} />);

    expect(screen.getByText("Additional Quotas")).toBeInTheDocument();
    expect(screen.getByText("GPT-5.3-Codex-Spark")).toBeInTheDocument();
    expect(screen.getByText(/35% used/)).toBeInTheDocument();
  });

  it("renders request log usage summary when available", () => {
    const account = createAccountSummary({
      requestUsage: {
        requestCount: 7,
        totalTokens: 51_480,
        cachedInputTokens: 41_470,
        totalCostUsd: 0.13,
      },
    });

    render(<AccountUsagePanel account={account} trends={null} />);

    expect(screen.getByText("Request logs total")).toBeInTheDocument();
    expect(screen.getByText(/\$0\.13/)).toBeInTheDocument();
    expect(screen.getByText(/51\.48K tok/)).toBeInTheDocument();
  });

  it("renders usage reset credit availability when provided", () => {
    const account = createAccountSummary();

    render(
      <AccountUsagePanel
        account={account}
        trends={null}
        resetCredits={{ availableCount: 3 }}
      />,
    );

    expect(screen.getByText("Usage resets")).toBeInTheDocument();
    expect(screen.getByText("3 available")).toBeInTheDocument();
  });

  it("shows the nearest reset-credit expiry when provided", () => {
    const expiresAt = "2026-01-01T02:34:56.000Z";
    const isoExpiry = formatDateTimeInline(expiresAt, "iso8601");
    const account = createAccountSummary({
      resetCreditNearestExpiresAt: expiresAt,
    });

    render(
      <AccountUsagePanel
        account={account}
        trends={null}
        resetCredits={{ availableCount: 3 }}
      />,
    );

    expect(
      screen.getByText((_, element) => {
        const text = element?.textContent ?? "";
        return text.startsWith("Expires ") && text.includes("(2h)") && !text.includes("available");
      }),
    ).toBeInTheDocument();
    const row = screen.getByText("Usage resets").closest("div");
    const rowText = row?.textContent ?? "";
    expect(rowText.indexOf("Expires ")).toBeGreaterThan(-1);
    expect(rowText.indexOf("Expires ")).toBeLessThan(rowText.indexOf("3 available"));
    expect(rowText).toContain(isoExpiry);

    act(() => {
      useDateDisplayFormatStore.setState({ dateDisplayFormat: "default" });
    });

    expect(row?.textContent).toContain(formatDateTimeInline(expiresAt, "default"));
    expect(row?.textContent).not.toContain(isoExpiry);
  });

  it("renders a usage reset action when provided", () => {
    const account = createAccountSummary({ accountId: "acc_reset" });
    const onReset = vi.fn();

    render(
      <AccountUsagePanel
        account={account}
        trends={null}
        resetCredits={{ availableCount: 1 }}
        onReset={onReset}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Reset usage" }));

    expect(onReset).toHaveBeenCalledWith("acc_reset");
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("shows the weekly plan legend when scheduled trend data exists", () => {
    const account = createAccountSummary();
    const trends = createAccountTrends(account.accountId, {
      secondaryScheduled: [
        { t: "2026-01-01T00:00:00.000Z", v: 100 },
        { t: "2026-01-01T01:00:00.000Z", v: 99.4 },
      ],
    });

    render(<AccountUsagePanel account={account} trends={trends} />);

    expect(screen.getByText("Weekly plan")).toBeInTheDocument();
  });

  it("shows trends when only secondary scheduled trend points are present", () => {
    const account = createAccountSummary();
    const trends = createAccountTrends(account.accountId, {
      primary: [],
      secondary: [],
      secondaryScheduled: [
        { t: "2026-01-01T00:00:00.000Z", v: 100 },
        { t: "2026-01-01T06:00:00.000Z", v: 92 },
      ],
    });

    render(<AccountUsagePanel account={account} trends={trends} />);

    expect(screen.getByText("7-day trend")).toBeInTheDocument();
    expect(screen.getByText("Weekly plan")).toBeInTheDocument();
  });

  it("preserves partial recorded subtotals without presenting wholly unknown observations or failed cached totals as zero", () => {
    const account = sourceToAccountView(createModelSource({ billingMode: "api_metered", provider: "openai" }));
    const usage = { sourceId: account.source!.id, windowStart: "2026-01-01T00:00:00Z", windowEnd: "2026-01-02T00:00:00Z", requestCount: 2, successCount: 0, errorCount: 2, cancelledCount: 0, inputTokens: null, outputTokens: null, cachedInputTokens: null, cacheCreationInputTokens: null, reasoningTokens: null, costUsd: null, knownCostUsd: null, unpricedRequestCount: 2, unknownUsageCount: 2, lastRequestAt: null };
    const { rerender } = render(<AccountUsagePanel account={account} sourceUsage={usage} />);
    const region = screen.getByRole("region", { name: "Observed gateway usage · last 24 hours" });
    expect(region).toHaveTextContent("Unknown tok");
    expect(region).not.toHaveTextContent("0 tok");
    fireEvent.click(within(region).getByText("Request and token details"));
    expect(within(region).getByText("Input tokens").nextElementSibling).toHaveTextContent("Unknown");
    expect(within(region).getByText("Cache creation tokens").nextElementSibling).toHaveTextContent("Unknown");
    rerender(<AccountUsagePanel account={account} sourceUsage={{ ...usage, requestCount: 1, unknownUsageCount: 1, inputTokens: 1200, cachedInputTokens: 100, cacheCreationInputTokens: 20, reasoningTokens: 10 }} />);
    expect(region).toHaveTextContent("1.2K tok");
    expect(region).toHaveTextContent("1 requests have unknown usage");
    expect(within(region).getByText("Input tokens").nextElementSibling).toHaveTextContent("1.2K");
    expect(within(region).getByText("Output tokens").nextElementSibling).toHaveTextContent("Unknown");
    expect(within(region).getByText("Cached input tokens").nextElementSibling).toHaveTextContent("100");
    expect(within(region).getByText("Cache creation tokens").nextElementSibling).toHaveTextContent("20");
    expect(within(region).getByText("Reasoning tokens").nextElementSibling).toHaveTextContent("10");
    rerender(<AccountUsagePanel account={account} sourceUsage={{ ...usage, inputTokens: 0, cachedInputTokens: 0 }} />);
    expect(region).toHaveTextContent("Unknown tok");
    expect(within(region).getByText("Input tokens").nextElementSibling).toHaveTextContent(/^0$/);
    expect(within(region).getByText("Cached input tokens").nextElementSibling).toHaveTextContent(/^0$/);
    expect(within(region).getByText("Output tokens").nextElementSibling).toHaveTextContent("Unknown");
    rerender(<AccountUsagePanel account={account} sourceUsage={{ ...usage, inputTokens: 9876, unknownUsageCount: 0 }} sourceUsageError />);
    expect(region).toHaveTextContent("Could not load observed gateway usage.");
    expect(region).not.toHaveTextContent("9.88K");
    expect(region).not.toHaveTextContent("$0.00");
  });

  it("separates upstream short-window allowance from recorded tokens without inventing an API balance", () => {
    const account = sourceToAccountView(createModelSource({ provider: "openai", billingMode: "api_metered" }));
    render(<AccountUsagePanel account={account} apiUsage={{ ...apiTelemetry, rateLimits: [...apiTelemetry.rateLimits, { ...apiTelemetry.rateLimits[0], tokenKind: "project_tokens", tokenLimit: 200000, tokensRemaining: 90000, requestLimit: null, requestsRemaining: null, requestsResetAt: null }] }} />);
    const telemetry = screen.getByRole("region", { name: "Provider API telemetry" });
    expect(telemetry).toHaveTextContent("76,543 remaining / 100,000 limit");
    expect(telemetry).toHaveTextContent("498 remaining / 500 limit");
    expect(within(telemetry).getByText("Rate-window project tokens").nextElementSibling).toHaveTextContent("90,000 remaining / 200,000 limit");
    expect(within(telemetry).getByText("Rate-window tokens").nextElementSibling).toHaveTextContent("76,543 remaining / 100,000 limit");
    expect(telemetry).toHaveTextContent("Reset:");
    expect(telemetry).toHaveTextContent("Observed:");
    expect(telemetry).toHaveTextContent("OpenAI and Anthropic API balances cannot be read here");
    expect(telemetry).not.toHaveTextContent("$0.00");
    expect(screen.queryByText("Estimated token allocation")).not.toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("expires rate observations at reset and does not resurrect cached allowance on error", () => {
    const account = sourceToAccountView(createModelSource({ billingMode: "api_metered" }));
    const { rerender } = render(<AccountUsagePanel account={account} apiUsage={{ ...apiTelemetry, rateLimits: [{ ...apiTelemetry.rateLimits[0], tokensResetAt: "2026-01-01T00:00:01Z", requestsResetAt: "2026-01-01T00:01:00Z" }] }} />);
    const telemetry = screen.getByRole("region", { name: "Provider API telemetry" });
    act(() => { vi.advanceTimersByTime(999); });
    expect(telemetry).toHaveTextContent("76,543 remaining / 100,000 limit");
    act(() => { vi.advanceTimersByTime(1); });
    expect(telemetry).toHaveTextContent("498 remaining / 500 limit");
    expect(telemetry).toHaveTextContent("Unknown remaining / 100,000 limit");
    expect(telemetry).toHaveTextContent("Expired or stale observation");
    expect(telemetry).not.toHaveTextContent("76,543 remaining");
    act(() => { vi.advanceTimersByTime(59_000); });
    expect(telemetry).toHaveTextContent("Unknown remaining / 500 limit");
    expect(telemetry).not.toHaveTextContent("498 remaining");
    rerender(<AccountUsagePanel account={account} apiUsage={apiTelemetry} apiUsageError />);
    expect(telemetry).toHaveTextContent("Could not load API telemetry");
    expect(telemetry).not.toHaveTextContent("76,543");
    expect(telemetry).not.toHaveTextContent("0 remaining");
  });

  it("keeps Anthropic input/output rate buckets separate and DeepSeek native currency distinct from tokens", () => {
    const account = sourceToAccountView(createModelSource({ provider: "deepseek", billingMode: "api_metered" }));
    const { rerender } = render(<AccountUsagePanel account={account} apiUsage={{ ...apiTelemetry, balanceStatus: "available", balanceObservedAt: "2026-01-01T00:00:00Z", balanceIsAvailable: true, balances: [{ currency: "CNY", totalBalance: "12.3400", grantedBalance: "0", toppedUpBalance: "12.3400" }], rateLimits: [
      { ...apiTelemetry.rateLimits[0], tokenKind: "input_tokens", tokensRemaining: 1234, requestLimit: null, requestsRemaining: null, requestsResetAt: null },
      { ...apiTelemetry.rateLimits[0], tokenKind: "output_tokens", tokensRemaining: null, requestLimit: null, requestsRemaining: null, requestsResetAt: null },
    ] }} />);
    const telemetry = screen.getByRole("region", { name: "Provider API telemetry" });
    expect(within(telemetry).getByText("Rate-window input tokens").nextElementSibling).toHaveTextContent("1,234 remaining");
    expect(within(telemetry).getByText("Rate-window output tokens").nextElementSibling).toHaveTextContent("Unknown remaining");
    expect(telemetry).toHaveTextContent("12.3400 CNY");
    expect(telemetry).not.toHaveTextContent("12.3400 tok");
    rerender(<AccountUsagePanel account={account} apiUsage={{ ...apiTelemetry, balanceStatus: "unknown", balanceError: "private-provider-detail" }} />);
    expect(telemetry).toHaveTextContent("Provider balance has not been read or could not be refreshed");
    expect(telemetry).not.toHaveTextContent("private-provider-detail");
    expect(telemetry).not.toHaveTextContent("12.3400 CNY");
  });

  it("shows a genuine native zero balance but never substitutes it for a stale observation", () => {
    const account = sourceToAccountView(createModelSource({ provider: "deepseek", billingMode: "api_metered" }));
    render(<AccountUsagePanel account={account} apiUsage={{ ...apiTelemetry, balanceStatus: "available", balanceObservedAt: "2026-01-01T00:00:00Z", balanceIsAvailable: false, balances: [{ currency: "USD", totalBalance: "0.00", grantedBalance: null, toppedUpBalance: "0.00" }] }} />);
    const telemetry = screen.getByRole("region", { name: "Provider API telemetry" });
    expect(telemetry).toHaveTextContent("0.00 USD");
    expect(telemetry).toHaveTextContent("Provider reports insufficient funds");
    act(() => { vi.advanceTimersByTime(330_000); });
    expect(telemetry).toHaveTextContent("Balance observation is stale");
    expect(telemetry).not.toHaveTextContent("0.00 USD");
  });
});
