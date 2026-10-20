import type { ReactNode } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DailyReportRow, ReportSummary } from "../schemas";
import { ReportsSummaryCards } from "./reports-summary-cards";
import { DailyDetailTable } from "./daily-detail-table";
import { ModelDistributionDonut } from "./model-distribution-donut";
import { UseragentDistributionDonut } from "./useragent-distribution-donut";
import { ReportSourceBreakdown } from "./report-source-breakdown";

vi.mock("@/components/lazy-recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  PieChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Pie: () => null, Cell: () => null, Sector: () => null,
}));
afterEach(() => vi.restoreAllMocks());
const summary: ReportSummary = {
  totalCostUsd: 0, unknownCostCount: 2, totalInputTokens: 100, totalOutputTokens: 20,
  totalReasoningTokens: 0, reasoningUsageKnownRequests: 0, totalCachedTokens: 0,
  totalRequests: 2, totalCancelled: 0, totalErrors: 0, totalConversations: 0,
  activeAccounts: 1, avgCostPerDay: 0, avgRequestsPerDay: 2,
};
const row: DailyReportRow = {
  date: "2026-10-01", requests: 2, conversations: 0, inputTokens: 100, outputTokens: 20,
  reasoningTokens: null, cachedInputTokens: 0, costUsd: 0, unknownCostCount: 2,
  activeAccounts: 1, cancelledCount: 0, errorCount: 0,
};

describe("report pricing consumers", () => {
  it("distinguishes positive sub-cent costs from free and unknown totals", () => {
    render(<><ReportsSummaryCards summary={{ ...summary, totalCostUsd: 0.0042, avgCostPerDay: 0.0042 }} comparison={{ canCompare: false, previous: { totalCostUsd: 0, totalTokens: 0, totalRequests: 0 } }} />
      <ModelDistributionDonut data={[{ model: "claude-opus-5-5", costUsd: 0.0042, requests: 1, percentage: 100, unknownCostCount: 0 }]} /></>);
    const card = screen.getByTestId("report-summary-card-total-cost");
    expect(card).toHaveTextContent("<$0.01");
    expect(card).not.toHaveTextContent("$0.00");
    expect(card).toHaveTextContent("2");
    expect(screen.getByTestId("model-distribution-center-value")).toHaveTextContent("<$0.01");
    expect(screen.getByTestId("model-distribution-legend-0")).toHaveTextContent("<$0.01");
  });
  it("labels unknown-only totals and daily averages without zero dollars or cost deltas", () => {
    render(<ReportsSummaryCards summary={summary} comparison={{ canCompare: true, previous: { totalCostUsd: 10, totalTokens: 120, totalRequests: 2 } }} />);
    const card = screen.getByTestId("report-summary-card-total-cost");
    expect(card).toHaveTextContent("API-equivalent estimate");
    expect(card).toHaveTextContent("Unknown");
    expect(card).not.toHaveTextContent("$0");
    expect(card).not.toHaveTextContent("%");
  });

  it.each(["current", "previous"])("discloses known subtotals and disables cost comparison for %s unknown pricing", (period) => {
    render(<ReportsSummaryCards summary={{ ...summary, totalCostUsd: 12, avgCostPerDay: 12, unknownCostCount: period === "current" ? 1 : 0 }} comparison={{ canCompare: true, previous: { totalCostUsd: 10, totalTokens: 120, totalRequests: 2, unknownCostCount: period === "previous" ? 1 : 0 } }} />);
    const card = screen.getByTestId("report-summary-card-total-cost");
    expect(card).toHaveTextContent("$12.00");
    if (period === "current") expect(card).toHaveTextContent(/unknown/i);
    expect(card).not.toHaveTextContent("%");
  });

  it("exports unknown costs as unknown and quotes mixed subtotals with thousands separators", async () => {
    let exported: Blob | undefined;
    vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => { exported = blob as Blob; return "blob:report"; });
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<DailyDetailTable startDate="2026-10-01" endDate="2026-10-02" data={[row, { ...row, date: "2026-10-02", costUsd: 1400 }]} />);
    expect(screen.getByTestId("daily-breakdown-row-2026-10-01")).toHaveTextContent("Unknown");
    expect(screen.getByTestId("daily-breakdown-row-2026-10-01")).not.toHaveTextContent("$0");
    fireEvent.click(screen.getByRole("button", { name: /CSV/i }));
    const csv = await exported!.text();
    expect(csv).toContain("API-equivalent estimate");
    expect(csv).toContain(',"Unknown",');
    expect(csv).toMatch(/,"\$1,400\.00[^"]+",/);
  });

  it("shows unknown model and User-Agent costs while retaining request metric toggles", () => {
    render(<><ModelDistributionDonut data={[{ model: "unpriced", costUsd: 0, requests: 2, percentage: 0, unknownCostCount: 2 }]} /><UseragentDistributionDonut data={[{ useragent: "CLI", costUsd: 0, requests: 2, percentage: 0, unknownCostCount: 2 }]} /></>);
    for (const kind of ["model", "useragent"]) {
      expect(screen.getByTestId(`${kind}-distribution-center-value`)).toHaveTextContent("Unknown");
      expect(screen.getByTestId(`${kind}-distribution-legend-0`)).toHaveTextContent("Unknown");
      expect(screen.getByTestId(`${kind}-distribution-legend-0`)).not.toHaveTextContent("$0");
    }
    fireEvent.click(screen.getAllByRole("button", { name: /^req$/i })[0]);
    expect(screen.getByTestId("model-distribution-center-value")).toHaveTextContent("2");
    expect(screen.getByTestId("useragent-distribution-center-value")).toHaveTextContent("Unknown");
  });

  it("keeps same-alias mixed accounts separate and includes unpriced providers in request distribution", async () => {
    render(<ReportSourceBreakdown byAccount={[
      { accountId: "codex-1", alias: "Shared", accountType: "codex", costUsd: 3, requests: 2 },
      { accountId: "source:claude-1", modelSourceId: "claude-1", alias: "Shared", accountType: "claude", costUsd: 0, requests: 2, unknownCostCount: 2 },
      { accountId: null, alias: null, accountType: "unknown", costUsd: 0, requests: 1, unknownCostCount: 1 },
    ]} byType={[
      { accountType: "codex", costUsd: 3, requests: 2 },
      { accountType: "claude", costUsd: 0, requests: 2, unknownCostCount: 2 },
      { accountType: "unknown", costUsd: 0, requests: 1, unknownCostCount: 1 },
    ]} />);
    const accounts = screen.getByRole("region", { name: "By account / source" });
    const types = await screen.findByRole("region", { name: "Distribution by Provider" });
    expect(within(accounts).getAllByText("Shared")).toHaveLength(2);
    expect(within(accounts).getByText("codex-1")).toBeInTheDocument();
    expect(within(accounts).getByText("source:claude-1")).toBeInTheDocument();
    expect(within(accounts).getByText("Unattributed / deleted account")).toBeInTheDocument();
    const claudeRow = within(accounts).getByText("source:claude-1").closest("tr")!;
    expect(claudeRow).toHaveTextContent("claude");
    expect(claudeRow).toHaveTextContent("Unknown");
    expect(claudeRow).not.toHaveTextContent("$0");
    expect(within(types).getByTestId("provider-distribution-legend-1")).toHaveTextContent("claude");
    expect(within(types).getByTestId("provider-distribution-legend-1")).toHaveTextContent("Unknown");
    expect(within(types).getByTestId("provider-distribution-legend-1")).not.toHaveTextContent("$0");
    fireEvent.click(within(types).getByRole("button", { name: /^req$/i }));
    expect(within(types).getByTestId("provider-distribution-center-value")).toHaveTextContent("5");
    expect(within(types).getByTestId("provider-distribution-legend-1")).toHaveTextContent("40.0%");
    expect(within(types).getByTestId("provider-distribution-legend-2")).toHaveTextContent("20.0%");
    fireEvent.click(within(types).getByRole("button", { name: /^cost$/i }));
    expect(within(types).getByTestId("provider-distribution-center-value")).toHaveTextContent("$3.00");
    expect(within(types).getByTestId("provider-distribution-center-value")).toHaveTextContent(/unknown/i);
    expect(within(types).getByTestId("provider-distribution-legend-1")).not.toHaveTextContent("0.0%");
  });
});
