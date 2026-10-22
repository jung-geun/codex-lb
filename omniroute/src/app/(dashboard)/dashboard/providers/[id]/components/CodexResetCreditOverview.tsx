"use client";

import { useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useCodexResetCreditOverview } from "./useCodexResetCreditOverview";

interface Props {
  connectionId: string;
  enabled?: boolean;
}

function formatRemaining(milliseconds: number, locale: string): string {
  const seconds = Math.max(1, Math.ceil(milliseconds / 1000));
  const day = Math.floor(seconds / 86400);
  const hour = Math.floor((seconds % 86400) / 3600);
  const minute = Math.floor((seconds % 3600) / 60);
  const unit = (value: number, name: "day" | "hour" | "minute" | "second") =>
    new Intl.NumberFormat(locale, { style: "unit", unit: name, unitDisplay: "short" }).format(
      value
    );
  if (day > 0) return `${unit(day, "day")} ${unit(hour, "hour")}`;
  if (hour > 0) return `${unit(hour, "hour")} ${unit(minute, "minute")}`;
  if (minute > 0) return `${unit(minute, "minute")} ${unit(seconds % 60, "second")}`;
  return unit(seconds, "second");
}

function Overview({ connectionId, enabled }: Required<Props>) {
  const t = useTranslations("providers");
  const locale = useLocale();
  const state = useCodexResetCreditOverview(connectionId, enabled);
  const [clock, setClock] = useState(() => Date.now());
  const hasExpiry = state.status === "loaded" && state.credits.some((c) => c.expiresAt !== null);
  useEffect(() => {
    if (!hasExpiry) return;
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [hasExpiry]);
  const dateFormat = new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
  const now = state.status === "loaded" ? Math.max(clock, state.observedAt) : clock;
  const missingDetails =
    state.status === "loaded" && state.reportedCount !== null
      ? Math.max(0, state.reportedCount - state.credits.length)
      : null;

  return (
    <section
      className="basis-full min-w-0 rounded-lg border border-border/50 p-3"
      data-testid="codex-reset-credit-overview"
      aria-label={t("codexResetOverviewTitle")}
      aria-busy={state.status === "loading"}
    >
      <h3 className="text-sm font-medium text-text-primary">{t("codexResetOverviewTitle")}</h3>
      <p className="mt-1 text-xs text-text-muted">{t("codexResetOverviewSeparate")}</p>
      {state.status === "loading" && (
        <p className="mt-2 text-xs text-text-muted" role="status">
          {t("codexResetOverviewLoading")}
        </p>
      )}
      {state.status === "unavailable" && (
        <p className="mt-2 text-xs text-text-muted" role="status">
          {t("codexResetOverviewUnavailable")}
        </p>
      )}
      {state.status === "loaded" && (
        <>
          <p className="mt-2 text-xs text-text-muted" data-testid="codex-reset-credit-count">
            {state.reportedCount === null
              ? t("codexResetOverviewCountUnknown")
              : t("codexResetOverviewCount", { count: state.reportedCount })}
            {" · "}
            {t("codexResetOverviewListed", { count: state.credits.length })}
          </p>
          {missingDetails !== null && missingDetails > 0 && (
            <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">
              {t("codexResetOverviewMissingDetails", { count: missingDetails })}
            </p>
          )}
          <ul className="mt-2 divide-y divide-border/50">
            {state.credits.map((credit, index) => {
              const expired = credit.expiresAt !== null && credit.expiresAt <= now;
              const soon =
                credit.expiresAt !== null && !expired && credit.expiresAt - now <= 86400000;
              return (
                <li key={index} className="py-2 text-xs" data-testid="codex-reset-credit-entry">
                  <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                    <span className="font-medium text-text-primary">
                      {t("codexResetOverviewCoupon", { number: index + 1 })}
                    </span>
                    <span
                      className={
                        expired
                          ? "text-text-muted"
                          : soon
                            ? "text-amber-600 dark:text-amber-400"
                            : "text-text-secondary"
                      }
                    >
                      {credit.expiresAt === null
                        ? t("codexResetOverviewExpiryUnknown")
                        : expired
                          ? t("codexResetOverviewExpired")
                          : soon
                            ? t("codexResetOverviewSoon")
                            : null}
                    </span>
                  </div>
                  {credit.grantedAt !== null && (
                    <p className="mt-1 text-text-muted">
                      {t("codexResetOverviewGranted", {
                        date: dateFormat.format(credit.grantedAt),
                      })}
                    </p>
                  )}
                  {credit.expiresAt !== null && (
                    <p className="mt-1 text-text-muted">
                      {t("codexResetOverviewExpires", {
                        date: dateFormat.format(credit.expiresAt),
                      })}
                      {!expired && (
                        <span className="ml-2 tabular-nums">
                          {t("codexResetOverviewRemaining", {
                            duration: formatRemaining(credit.expiresAt - now, locale),
                          })}
                        </span>
                      )}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
          <p className="mt-1 text-xs text-text-muted">{t("codexResetOverviewSnapshot")}</p>
        </>
      )}
    </section>
  );
}

export default function CodexResetCreditOverview({ connectionId, enabled = true }: Props) {
  return (
    <Overview key={`${connectionId}:${enabled}`} connectionId={connectionId} enabled={enabled} />
  );
}
