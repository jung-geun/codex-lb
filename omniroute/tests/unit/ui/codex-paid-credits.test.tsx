// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.unmock("next-intl");
import { NextIntlClientProvider } from "next-intl";
import en from "@/i18n/messages/en.json";
import ko from "@/i18n/messages/ko.json";
import SubscriptionCreditStatus from "@/app/(dashboard)/dashboard/usage/components/ProviderLimits/parts/SubscriptionCreditStatus";
import ConnectionQuotaPanel from "@/app/(dashboard)/dashboard/providers/[id]/components/ConnectionQuotaPanel";
import QuotaCardExpanded from "@/app/(dashboard)/dashboard/usage/components/ProviderLimits/parts/QuotaCardExpanded";
import { parseQuotaData } from "@/app/(dashboard)/dashboard/usage/components/ProviderLimits/quotaParsing";
import QuotaCard from "@/app/(dashboard)/dashboard/usage/components/ProviderLimits/QuotaCard";

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); });

async function mount(element: React.ReactNode, locale: "en" | "ko" = "en") {
  await act(async () => root.render(
    <NextIntlClientProvider locale={locale} messages={locale === "en" ? en : ko} timeZone="UTC">
      {element}
    </NextIntlClientProvider>
  ));
  return container.textContent!;
}
const credits = { hasCredits: true, unlimited: false, overageLimitReached: false, balance: 1234.56789 };
const fetchedAt = () => new Date().toISOString();

describe("provider-reported subscription credit metadata", () => {
  for (const locale of ["en", "ko"] as const) {
    const text = locale === "en" ? en.providers : ko.providers;
    it("displays real credit units and precision without API-dollar conversion (" + locale + ")", async () => {
      const output = await mount(<ConnectionQuotaPanel providerId="codex"
        connection={{ id: "one", provider: "codex", providerSpecificData: { allowPaidCredits: false } }}
        cache={{ quotas: {}, paidCredits: credits, fetchedAt: fetchedAt() }} />, locale);
      expect(output).toContain(text.subscriptionCreditBalance);
      expect(output).toContain(new Intl.NumberFormat(locale, { maximumFractionDigits: 20 }).format(credits.balance));
      expect(output).not.toContain("$");
      expect(output).toContain(text.subscriptionCreditProviderReported);
    });

    it("keeps unknown balances distinct from zero (" + locale + ")", async () => {
      let output = await mount(<SubscriptionCreditStatus provider="codex"
        snapshot={{ paidCredits: { ...credits, balance: null }, fetchedAt: fetchedAt() }} />, locale);
      expect(output).toContain(text.subscriptionCreditUnknown);
      expect(output).not.toContain("0 " + (locale === "ko" ? "크레딧" : "credits"));
      output = await mount(<SubscriptionCreditStatus provider="codex"
        snapshot={{ paidCredits: { ...credits, balance: 0 }, fetchedAt: fetchedAt() }} />, locale);
      expect(output).toContain("0 " + (locale === "ko" ? "크레딧" : "credits"));
      expect(output).toContain(text.subscriptionCreditNone);
    });

    it("does not turn missing Claude limits into wallet funds or zero (" + locale + ")", async () => {
      const output = await mount(<SubscriptionCreditStatus provider="claude"
        snapshot={{ extraUsage: { is_enabled: true, used_credits: null }, fetchedAt: fetchedAt() }} />, locale);
      expect(output).toContain(text.subscriptionCreditUnknown);
      expect(output).toContain(text.subscriptionCreditRemainingLimit);
      expect(output).toContain(text.subscriptionCreditNotWallet);
      expect(output).not.toContain("$");
      expect(output).not.toContain("0 " + (locale === "ko" ? "크레딧" : "credits"));
    });
  }

  it("does not round a small positive credit balance into zero", async () => {
    const output = await mount(<SubscriptionCreditStatus provider="codex"
      snapshot={{ paidCredits: { ...credits, balance: 0.000000123 }, fetchedAt: fetchedAt() }} />);
    expect(output).toContain("0.000000123 credits");
    expect(output).not.toContain(en.providers.subscriptionCreditNone);
  });

  it("shows over-limit separately even when a balance remains", async () => {
    const output = await mount(<SubscriptionCreditStatus provider="codex"
      snapshot={{ paidCredits: { ...credits, overageLimitReached: true }, fetchedAt: fetchedAt() }} />);
    expect(output).toContain(en.providers.subscriptionCreditLimitReached);
    expect(output).toContain("1,234.56789 credits");
  });

  it("labels old snapshots stale without hiding their last reported balance", async () => {
    const output = await mount(<SubscriptionCreditStatus provider="codex"
      snapshot={{ paidCredits: credits, fetchedAt: "2020-01-01T00:00:00Z" }} />);
    expect(output).toContain(en.providers.subscriptionCreditStale);
    expect(output).toContain("1,234.56789 credits");
    expect(output).not.toContain(en.providers.subscriptionCreditProviderReported);
  });

  it("shows unknown actual account entitlement instead of inventing available credits", async () => {
    const output = await mount(<SubscriptionCreditStatus provider="codex"
      snapshot={{ fetchedAt: fetchedAt() }} />);
    expect(output).toContain(en.providers.subscriptionCreditEntitlementUnknown);
    expect(output).toContain(en.providers.subscriptionCreditUnknown);
  });

  it("honors Claude decimal units and currency but calls the result a configured limit", async () => {
    const output = await mount(<SubscriptionCreditStatus provider="claude" snapshot={{
      extraUsage: { is_enabled: true, monthly_limit: 9000, used_credits: 1234, currency: "EUR", decimal_places: 2 },
      fetchedAt: fetchedAt(),
    }} />);
    expect(output).toContain("77.66 EUR");
    expect(output).toContain(en.providers.subscriptionCreditRemainingLimit);
    expect(output).toContain(en.providers.subscriptionCreditNotWallet);
    expect(output).not.toContain("$");
  });

  it("distinguishes a zero Claude limit from a missing limit", async () => {
    const output = await mount(<SubscriptionCreditStatus provider="claude" snapshot={{
      extraUsage: { is_enabled: true, monthly_limit: 0, used_credits: 0, decimal_places: 0 }, fetchedAt: fetchedAt(),
    }} />);
    expect(output).toContain("0 credits");
    expect(output).toContain(en.providers.subscriptionCreditLimitReached);
    expect(output).not.toContain(en.providers.subscriptionCreditUnknown);
  });

  it("preserves unknown Claude amounts in the existing usage parser and renderer", async () => {
    const quotas = parseQuotaData("claude", { extraUsage: { is_enabled: true, monthly_limit: null, used_credits: null } });
    const output = await mount(<QuotaCardExpanded providerId="claude" quotas={quotas}
      loading={false} error={null} hasStaleData={false} canEditCutoff={false}
      hasCutoffOverrides={false} onRefresh={() => {}} onOpenCutoff={() => {}} onOpenCost={() => {}} />);
    expect(output).toContain(en.providers.subscriptionCreditRemainingLimit);
    expect(output).toContain(en.providers.subscriptionCreditUnknown);
    expect(output).not.toContain("$0");
  });

  it("honors unlimited entitlement even when the numeric credit counter is zero", async () => {
    const output = await mount(<SubscriptionCreditStatus provider="codex"
      snapshot={{ paidCredits: { ...credits, unlimited: true, balance: 0 }, fetchedAt: fetchedAt() }} />);
    expect(output).toContain(en.providers.quotaUnlimited);
    expect(output).toContain(en.providers.subscriptionCreditProviderReported);
    expect(output).not.toContain(en.providers.subscriptionCreditNone);
  });

  it("does not mislabel disabled Claude extra usage as an exhausted spending cap", async () => {
    const output = await mount(<SubscriptionCreditStatus provider="claude" snapshot={{
      extraUsage: { is_enabled: false, monthly_limit: 0, used_credits: 0 }, fetchedAt: fetchedAt(),
    }} />);
    expect(output).toContain(en.providers.subscriptionCreditProviderDisabled);
    expect(output).not.toContain(en.providers.subscriptionCreditLimitReached);
  });

  it("does not call a future-dated credit observation currently usable", async () => {
    const output = await mount(<SubscriptionCreditStatus provider="codex" snapshot={{
      paidCredits: credits, fetchedAt: new Date(Date.now() + 120_000).toISOString(),
    }} />);
    expect(output).toContain(en.providers.subscriptionCreditStale);
    expect(output).not.toContain(en.providers.subscriptionCreditProviderReported);
  });

  it("keeps a cached stale usage card historical even when its download timestamp is recent", async () => {
    await mount(<QuotaCard connection={{ id: "cached", provider: "codex", name: "Offline fixture" }}
      quota={{ quotas: [], raw: { paidCredits: credits, fetchedAt: fetchedAt(), _stale: true } }}
      loading={false} error={null} refreshedAt={fetchedAt()} emailsVisible={false}
      providerLabel="Codex" onRefresh={() => {}} onOpenCutoff={() => {}}
      onToggleActive={() => {}} togglingActive={false} />);
    const output = container.querySelector('[data-testid="subscription-credit-status"]')!.textContent!;
    expect(output).toContain("1,234.56789 credits");
    expect(output).toContain(en.providers.subscriptionCreditStale);
    expect(output).not.toContain(en.providers.subscriptionCreditProviderReported);
  });
});
