"use client";

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Button, Input } from "@/shared/components";

type CreditSpend = { day: number; week: number; trackedSince: number };

const positiveLimit = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;

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
  const locale = useLocale();
  const data = (providerSpecificData ?? {}) as Record<string, unknown>;
  const initial = { daily: positiveLimit(data.creditDailyLimit), weekly: positiveLimit(data.creditWeeklyLimit) };
  const [saved, setSaved] = useState(initial);
  const [daily, setDaily] = useState(initial.daily === null ? "" : String(initial.daily));
  const [weekly, setWeekly] = useState(initial.weekly === null ? "" : String(initial.weekly));
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "failed" | "invalid">("idle");
  const number = new Intl.NumberFormat(locale, { maximumFractionDigits: 6 });
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
  const spentLine = (window: "daily" | "weekly", spent: number | undefined, limit: number | null) =>
    t("codexCreditSpent", {
      window: t(window === "daily" ? "codexCreditWindowDaily" : "codexCreditWindowWeekly"),
      spent: spent === undefined ? t("subscriptionCreditUnknown") : number.format(spent),
      limit: limit === null ? t("codexCreditNoLimit") : number.format(limit),
    });
  const youngTracker = creditSpend && renderedAt - creditSpend.trackedSince < 7 * 24 * 60 * 60 * 1000;

  return (
    <div className="mt-3 border-t border-border/40 pt-3" data-testid={`codex-credit-limits-${connectionId}`}>
      <p className="text-xs font-semibold text-text-main">{t("codexCreditLimitTitle")}</p>
      <p className="mt-1 text-xs text-text-muted">{t("codexCreditLimitHelp")}</p>
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
      <div className="mt-2 space-y-0.5 text-xs text-text-muted" data-testid="codex-credit-spend">
        <p>{spentLine("daily", creditSpend?.day, saved.daily)}</p>
        <p>{spentLine("weekly", creditSpend?.week, saved.weekly)}</p>
        {youngTracker && <p>{t("codexCreditSpendLowerBound")}</p>}
      </div>
    </div>
  );
}
