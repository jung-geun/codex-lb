// @vitest-environment jsdom
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
vi.unmock("next-intl");
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import en from "@/i18n/messages/en.json";
import ko from "@/i18n/messages/ko.json";
import { UsageCostSummary } from "@/shared/components/UsageStats";
import BudgetTab from "@/app/(dashboard)/dashboard/usage/components/BudgetTab";

function renderView(node: React.ReactNode) {
  return renderToStaticMarkup(<NextIntlClientProvider locale="en" messages={en}
    timeZone="UTC" now={new Date("2026-10-22T00:00:00.000Z")}>{node}</NextIntlClientProvider>);
}

const mounted: Array<{ root: Root; container: HTMLDivElement }> = [];
afterEach(() => {
  for (const { root, container } of mounted.splice(0)) {
    act(() => root.unmount());
    container.remove();
  }
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function renderBudget(amount: number, locale: "en" | "ko" = "en") {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
  localStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => {
    const url = String(input);
    if (url === "/api/keys") return Response.json({ keys: [{ id: "own", name: "Offline budget fixture" }] });
    if (url === "/api/usage/budget/bulk") return Response.json({ budgets: { own: {
      totalCostToday: amount, totalCostMonth: amount, dailyLimitUsd: 1, monthlyLimitUsd: 5,
    } } });
    if (url === "/api/usage/budget?apiKeyId=own") return Response.json({ ledgerDebits: {
      amountUsd: amount, requestCount: amount > 0 ? 1 : 0,
      byProvider: amount > 0 ? [{ provider: "codex", amountUsd: amount, requestCount: 1 }] : [],
      costBasis: "api_equivalent_budget_debit", actualChargeUsd: null,
      billingRungAttribution: "unavailable", coverage: "retained_cost_ledger_only",
    } });
    throw new Error("Unexpected budget fixture request: " + url);
  }));
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () => root.render(<NextIntlClientProvider locale={locale} messages={locale === "ko" ? ko : en}
    timeZone="UTC"><BudgetTab /></NextIntlClientProvider>));
  await act(async () => { await vi.runOnlyPendingTimersAsync(); });
  const expand = container.querySelector<HTMLButtonElement>('button[aria-expanded="false"]');
  expect(expand).not.toBeNull();
  await act(async () => { expand!.click(); });
  await act(async () => { await vi.runOnlyPendingTimersAsync(); });
  return container;
}

function providerDebit(container: HTMLElement) {
  return Array.from(container.querySelectorAll("span"))
    .find((element) => element.textContent === "codex")?.parentElement?.textContent;
}

describe("native credit usage evidence", () => {
  it("separates a positive key budget estimate from unobserved native actual charges", () => {
    const html = renderView(<UsageCostSummary stats={{ totalCost: 0, nativeUsageRequests: 2,
      nativeBudgetDebits: { amountUsd: 0.12, requestCount: 1 } }} />);
    expect(html).toContain("$0.12");
    expect(html).toContain(en.stats.nativeActualChargeUnknown);
    expect(html).toContain(en.stats.recordedCostExcludingNative);
    expect(html).toContain(en.stats.nativeLedgerCoverage);
  });

  it("does not fabricate an estimate when no per-request debit survived", () => {
    const html = renderView(<UsageCostSummary stats={{ totalCost: 0, nativeUsageRequests: 1,
      nativeBudgetDebits: { amountUsd: 0, requestCount: 0 } }} />);
    expect(html).toContain(en.stats.nativeActualChargeUnknown);
    expect(html).toContain(en.stats.nativeLedgerCoverage);
    expect(html).not.toContain(en.stats.nativeBudgetEstimate.split("{amount}")[0]);
  });

  it("retains ordinary non-native cost reporting", () => {
    const html = renderView(<UsageCostSummary stats={{ totalCost: 1.25 }} />);
    expect(html).toContain("$1.25");
    expect(html).not.toContain(en.stats.nativeActualChargeUnknown);
  });

  it("does not round a positive sub-cent ledger debit into zero", async () => {
    const container = await renderBudget(0.001);
    expect(providerDebit(container)).toContain("<$0.01");
    expect(providerDebit(container)).not.toContain("$0.00");
    expect(container.textContent).toContain(en.usage.budgetLedgerCoverage);
  });

  it("renders actual retained provider debits with Korean estimate and coverage context", async () => {
    const container = await renderBudget(0.12, "ko");
    expect(providerDebit(container)).toContain("0.12");
    expect(container.textContent).toContain(ko.usage.budgetLedgerCoverage);
  });

  it("reports missing ledger records rather than confirmed no provider spend", async () => {
    const container = await renderBudget(0);
    expect(providerDebit(container)).toBeUndefined();
    expect(container.textContent).toContain(en.usage.budgetLedgerCoverage);
    expect(container.textContent).not.toContain(en.usage.noSpendLast30Days);
  });
});
