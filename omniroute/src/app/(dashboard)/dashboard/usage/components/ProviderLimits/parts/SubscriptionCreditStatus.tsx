"use client";

import { useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { parseCodexPaidCredits } from "@/lib/providers/codexPaidCredits";
import { parseClaudeExtraUsageQuota } from "../quotaParsing";

interface CreditSnapshot {
  paidCredits?: unknown;
  extraUsage?: unknown;
  quotas?: Record<string, unknown> | null;
  modelQuotas?: Record<string, unknown> | null;
  stale?: boolean;
  _stale?: boolean;
  message?: string | null;
  fetchedAt?: string;
  observedAt?: number;
}

/** Provider-reported metadata, never account consent or a promise of spend eligibility. */
export default function SubscriptionCreditStatus({
  provider,
  snapshot,
}: {
  provider: string;
  snapshot?: CreditSnapshot | null;
}) {
  const t = useTranslations("providers");
  const locale = useLocale();
  const [clock, setClock] = useState<number | null>(null);
  useEffect(() => {
    const update = () => setClock(Date.now());
    update();
    const timer = setInterval(update, 15_000);
    return () => clearInterval(timer);
  }, []);
  if (!["codex", "claude", "antigravity", "agy"].includes(provider)) return null;
  const number = (value: number) =>
    new Intl.NumberFormat(locale, {
      maximumFractionDigits: 20,
    }).format(value);
  const observedAt = snapshot?.observedAt ?? Date.parse(snapshot?.fetchedAt || "");
  // Historical balances remain visible; freshness never grants permission to spend.
  const stale =
    !!snapshot &&
    (snapshot.stale === true ||
      snapshot._stale === true ||
      !!snapshot.message ||
      (clock !== null &&
        (!Number.isFinite(observedAt) ||
          clock - observedAt > 65_000 ||
          observedAt - clock > 30_000)));
  let amount = t("subscriptionCreditUnknown");
  let state = t("subscriptionCreditEntitlementUnknown");
  let label = t("subscriptionCreditBalance");
  if (provider === "codex") {
    const credits = parseCodexPaidCredits(snapshot?.paidCredits);
    if (credits?.unlimited) {
      amount = t("quotaUnlimited");
    } else if (credits?.balance !== null && credits?.balance !== undefined) {
      amount = t("quotaCredits", { count: number(credits.balance) });
    }
    if (credits?.overageLimitReached) state = t("subscriptionCreditLimitReached");
    else if (!credits?.unlimited && credits?.balance === 0) state = t("subscriptionCreditNone");
    else if (credits?.hasCredits === true || credits?.unlimited) {
      state = t("subscriptionCreditProviderReported");
    }
  } else if (provider === "claude") {
    label = t("subscriptionCreditRemainingLimit");
    const extra =
      snapshot?.extraUsage && typeof snapshot.extraUsage === "object"
        ? (snapshot.extraUsage as Record<string, unknown>)
        : null;
    const allowance = parseClaudeExtraUsageQuota(extra || {});
    const limitReached =
      allowance.total !== null &&
      (allowance.total === 0 || (allowance.used !== null && allowance.used >= allowance.total));
    if (allowance.creditCount !== null) {
      const currency =
        typeof allowance.currency === "string" && allowance.currency.trim()
          ? allowance.currency.trim()
          : null;
      amount = currency
        ? `${number(allowance.creditCount)} ${currency}`
        : t("quotaCredits", { count: number(allowance.creditCount) });
    }
    if (extra?.is_enabled === false) state = t("subscriptionCreditProviderDisabled");
    else if (limitReached) state = t("subscriptionCreditLimitReached");
    else if (extra?.is_enabled === true) state = t("subscriptionCreditProviderReported");
  } else {
    const raw = snapshot?.quotas?.credits ?? snapshot?.modelQuotas?.credits;
    const credits = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : null;
    const balance = credits?.remaining;
    if (typeof balance === "number" && Number.isFinite(balance) && balance >= 0) {
      amount = t("quotaCredits", { count: number(balance) });
      state = balance === 0 ? t("subscriptionCreditNone") : t("subscriptionCreditProviderReported");
    }
  }
  return (
    <div
      className="mt-3 space-y-1 rounded-lg border border-border/50 bg-surface-secondary/30 p-3 text-xs text-text-muted"
      data-testid="subscription-credit-status"
    >
      <p className="font-medium text-text-main">{label}</p>
      <p
        className="break-words text-lg font-semibold tabular-nums text-text-main"
        data-testid="subscription-credit-balance"
      >
        {amount}
      </p>
      <p>{stale ? t("subscriptionCreditStale") : state}</p>
      {provider === "claude" && <p>{t("subscriptionCreditNotWallet")}</p>}
    </div>
  );
}
