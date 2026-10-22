"use client";

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Button, Input } from "@/shared/components";

type CreditSpend = { day: number; week: number; trackedSince: number };

const positiveLimit = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;

function CreditUsageGraph({
  window,
  spent: observation,
  limit,
}: {
  window: "daily" | "weekly";
  spent: number | undefined;
  limit: number | null;
}) {
  const t = useTranslations("providers");
  const locale = useLocale();
  const number = new Intl.NumberFormat(locale, { maximumFractionDigits: 2 });
  const scientific = new Intl.NumberFormat(locale, {
    maximumSignificantDigits: 8,
    notation: "scientific",
  });
  const format = (value: number) =>
    (value !== 0 && (value < 0.01 || value >= 1e12) ? scientific : number).format(value);
  const spent = typeof observation === "number" && Number.isFinite(observation) && observation >= 0
    ? observation
    : null;
  const known = spent !== null && limit !== null;
  const overLimit = known && spent > limit;
  const windowLabel = t(window === "daily" ? "codexCreditWindowDaily" : "codexCreditWindowWeekly");
  const amountLabel = t("codexCreditSpent", {
    window: windowLabel,
    spent: spent === null ? t("subscriptionCreditUnknown") : format(spent),
    limit: limit === null ? t("codexCreditNoLimit") : format(limit),
  });
  let percentLabel: string | null = null;
  if (known) {
    const percentage = (spent / limit) * 100;
    let percent: string;
    if (Number.isFinite(percentage) && (percentage > 0 || spent === 0)) {
      percent = format(percentage);
    } else {
      // Keep even extreme finite observations from becoming a false zero or infinity.
      const logarithm = Math.log10(spent) - Math.log10(limit) + 2;
      const exponent = Math.floor(logarithm);
      percent = `${format(10 ** (logarithm - exponent))}E${exponent}`;
    }
    percentLabel = t("codexCreditUsagePercent", { percent });
  }
  const stateLabel = !known
    ? t(spent === null ? "codexCreditUsageUnavailable" : "codexCreditUsageUncapped")
    : overLimit
      ? t("codexCreditUsageOverLimit", { over: format(spent - limit) })
      : spent === limit
        ? t("codexCreditUsageLimitReached")
        : t("codexCreditUsageRemaining", { remaining: format(limit - spent) });

  return (
    <div
      role="group"
      aria-label={windowLabel}
      className="min-w-0 space-y-1.5"
      data-testid={`codex-credit-${window}-usage`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 tabular-nums">
        <p className="min-w-0 break-words text-text-main">{amountLabel}</p>
        {percentLabel && <p className="text-text-main">{percentLabel}</p>}
      </div>
      {known ? (
        <div
          role="progressbar"
          aria-label={windowLabel}
          aria-valuemin={0}
          aria-valuemax={limit}
          aria-valuenow={Math.min(spent, limit)}
          aria-valuetext={`${amountLabel}; ${percentLabel}; ${stateLabel}`}
          className="h-2 overflow-hidden rounded-full bg-border/60"
        >
          <div
            className={`h-full rounded-full ${spent >= limit ? "bg-error" : "bg-accent"}`}
            style={{ width: `${Math.min(100, (spent / limit) * 100)}%` }}
          />
        </div>
      ) : (
        <div aria-hidden="true" className="h-2 rounded-full border border-dashed border-border" />
      )}
      <p className="break-words tabular-nums text-text-muted">{stateLabel}</p>
    </div>
  );
}

/** Per-account Codex credit consumption limits; saves only its own two settings. */
export default function CodexCreditLimitFields({
  connectionId,
  providerSpecificData,
  creditSpend,
}: {
  connectionId: string;
  providerSpecificData: unknown;
  creditSpend?: CreditSpend | null;
}) {
  const t = useTranslations("providers");
  const data = (providerSpecificData ?? {}) as Record<string, unknown>;
  const initial = { daily: positiveLimit(data.creditDailyLimit), weekly: positiveLimit(data.creditWeeklyLimit) };
  const [saved, setSaved] = useState(initial);
  const [daily, setDaily] = useState(initial.daily === null ? "" : String(initial.daily));
  const [weekly, setWeekly] = useState(initial.weekly === null ? "" : String(initial.weekly));
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "failed" | "invalid">("idle");
  const [renderedAt] = useState(() => Date.now());

  const parse = (value: string): number | null | undefined => {
    if (!value.trim()) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined;
  };
  const save = async () => {
    const nextDaily = parse(daily);
    const nextWeekly = parse(weekly);
    if (nextDaily === undefined || nextWeekly === undefined) { setStatus("invalid"); return; }
    setStatus("saving");
    try {
      const res = await fetch(`/api/providers/${connectionId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ providerSpecificData: { creditDailyLimit: nextDaily, creditWeeklyLimit: nextWeekly } }),
      });
      if (!res.ok) { setStatus("failed"); return; }
      setSaved({ daily: nextDaily, weekly: nextWeekly });
      setStatus("saved");
    } catch {
      setStatus("failed");
    }
  };
  const youngTracker = creditSpend && renderedAt - creditSpend.trackedSince < 7 * 24 * 60 * 60 * 1000;

  return (
    <div className="mt-3 border-t border-border/40 pt-3" data-testid={`codex-credit-limits-${connectionId}`}>
      <p className="text-xs font-semibold text-text-main">{t("codexCreditLimitTitle")}</p>
      <p className="mt-1 text-xs text-text-muted">{t("codexCreditLimitHelp")}</p>
      <div className="mt-3 space-y-3 text-xs" data-testid="codex-credit-spend">
        <CreditUsageGraph window="daily" spent={creditSpend?.day} limit={saved.daily} />
        <CreditUsageGraph window="weekly" spent={creditSpend?.week} limit={saved.weekly} />
        {youngTracker && <p className="text-text-muted">{t("codexCreditSpendLowerBound")}</p>}
      </div>
      <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
        <Input label={t("codexCreditDailyLimit")} type="number" min="0" step="any" value={daily}
          placeholder={t("codexCreditNoLimit")} onChange={(event) => { setDaily(event.target.value); setStatus("idle"); }} />
        <Input label={t("codexCreditWeeklyLimit")} type="number" min="0" step="any" value={weekly}
          placeholder={t("codexCreditNoLimit")} onChange={(event) => { setWeekly(event.target.value); setStatus("idle"); }} />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="secondary" onClick={() => void save()} disabled={status === "saving"}>
          {t("codexCreditLimitSave")}
        </Button>
        {status === "saved" && <span className="text-xs text-emerald-500">{t("codexCreditLimitSaved")}</span>}
        {status === "failed" && <span className="text-xs text-red-500">{t("codexCreditLimitFailed")}</span>}
        {status === "invalid" && <span className="text-xs text-red-500">{t("codexCreditLimitInvalid")}</span>}
      </div>
    </div>
  );
}
