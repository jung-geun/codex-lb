"use client";

/**
 * ConnectionQuotaPanel — per-account usage/limits strip rendered under each
 * connection row on the provider detail page.
 *
 * Data comes entirely from the server-owned `providerLimitsCache` (loaded by
 * useProviderQuota). Parsing and window ordering reuse the Usage page's
 * quotaParsing module — the same code path that feeds Provider Limits — so
 * the numbers here can never drift from that page. Cross-page import of that
 * module follows the established pattern (home/ProviderQuotaWidget.tsx).
 */

import { useEffect, useId, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import type { CapacityEstimate } from "@/lib/services/quotaCapacity";
import {
  parseQuotaData,
  sortQuotasByWindow,
  hasCanonicalWindowOrder,
  hasFixedQuotaOrder,
  quotaWindowRank,
} from "../../../usage/components/ProviderLimits/quotaParsing";
import {
  getBarColor,
  formatCountdown,
  formatQuotaLabel,
} from "../../../usage/components/ProviderLimits/utils";
import type { ProviderQuotaCacheEntry } from "../hooks/useProviderQuota";
import SubscriptionCreditStatus from "../../../usage/components/ProviderLimits/parts/SubscriptionCreditStatus";
import type { ConnectionRowConnection } from "./ConnectionRow";

export interface ConnectionQuotaPanelProps {
  providerId: string;
  connection: ConnectionRowConnection;
  cache?: ProviderQuotaCacheEntry | null;
  refreshing?: boolean;
  onRefresh?: () => void;
}

interface QuotaRowLike {
  name: string;
  used?: number;
  total?: number;
  remaining?: number;
  remainingPercentage?: number;
  resetAt?: string | null;
  displayName?: string;
  isExtraUsageLimit?: boolean;
  currency?: string;
  isCredits?: boolean;
  isResetCredits?: boolean;
  isPercentageOnly?: boolean;
  fractionReported?: boolean;
  staleAfterReset?: boolean;
  creditCount?: number;
  unlimited?: boolean;
  message?: string;
}

function usedPercent(row: QuotaRowLike): number | null {
  if (typeof row.remainingPercentage === "number" && Number.isFinite(row.remainingPercentage)) {
    return Math.min(100, Math.max(0, Math.round(100 - row.remainingPercentage)));
  }
  const total = Number(row.total || 0);
  const used = Number(row.used || 0);
  if (total > 0) return Math.min(100, Math.max(0, Math.round((used / total) * 100)));
  return null;
}

function remainingOf(row: QuotaRowLike): number {
  if (typeof row.remainingPercentage === "number" && Number.isFinite(row.remainingPercentage)) {
    return row.remainingPercentage;
  }
  const total = Number(row.total || 0);
  const used = Number(row.used || 0);
  return total > 0 ? (1 - used / total) * 100 : 100;
}

// How many non-window (per-model) rows become chips: more when they are the
// only signal the provider reports, fewer when they supplement 5h/7d windows.
const MAX_MODEL_CHIPS_ALONE = 3;
const MAX_MODEL_CHIPS_WITH_WINDOWS = 1;

const MODEL_CHIP_LABEL_MAX = 20;

/** Compact chip label for a per-model bucket — truncated, full name in title. */
function modelChipLabel(name: string): string {
  return name.length > MODEL_CHIP_LABEL_MAX ? `${name.slice(0, MODEL_CHIP_LABEL_MAX - 1)}…` : name;
}

/** Compact chip label for a quota window: "5h", "7d", "Monthly", "Spark 5h". */
function shortWindowLabel(name: string, monthlyLabel: string, sparkLabel: string): string {
  const key = name.trim().toLowerCase();
  const spark = /spark/.test(key) ? `${sparkLabel} ` : "";
  if (/month/.test(key)) return monthlyLabel;
  if (/week|7\s*d\b|_7d\b|seven[_\s-]?day/.test(key)) return `${spark}7d`;
  if (/session|hour|\b5\s*h\b|_5h\b/.test(key)) return `${spark}5h`;
  return name;
}

function formatAgo(iso: string | null | undefined, locale: string): string | null {
  if (!iso) return null;
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const minutes = Math.round(ms / 60_000);
  if (minutes < 1) return rtf.format(0, "minute");
  if (minutes < 60) return rtf.format(-minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (hours < 24) return rtf.format(-hours, "hour");
  return rtf.format(-Math.round(hours / 24), "day");
}

export default function ConnectionQuotaPanel({
  providerId,
  connection,
  cache,
  refreshing,
  onRefresh,
}: ConnectionQuotaPanelProps) {
  const t = useTranslations("providers");
  const locale = useLocale();
  const sectionId = useId();
  const providerKey = String(connection.provider || providerId || "").toLowerCase();
  const showSubscriptionDetails = ["codex", "claude", "antigravity", "agy"].includes(providerKey);
  const [clock, setClock] = useState(() => Date.now());
  const [expanded, setExpanded] = useState(false);
  const [capacityResult, setCapacityResult] = useState<{
    connectionId: string;
    windows: CapacityEstimate[];
    error: boolean;
  } | null>(null);
  const currentCapacity = capacityResult?.connectionId === connection.id ? capacityResult : null;
  const capacity = currentCapacity?.windows ?? null;
  const capacityError = currentCapacity?.error ?? false;
  const capacityLoading = expanded && currentCapacity === null;

  // Keep reset countdowns and snapshot age current without fetching provider data.
  useEffect(() => {
    if (!showSubscriptionDetails) return;
    const timer = setInterval(() => setClock(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, [showSubscriptionDetails]);

  useEffect(() => {
    if (!expanded || !connection.id) return;
    const controller = new AbortController();
    fetch(`/api/providers/${encodeURIComponent(connection.id)}/quota-capacity`, {
      signal: controller.signal,
      cache: "no-store",
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("quota capacity request failed");
        const data = (await response.json()) as { windows?: CapacityEstimate[] };
        if (!controller.signal.aborted)
          setCapacityResult({
            connectionId: connection.id,
            windows: Array.isArray(data.windows) ? data.windows : [],
            error: false,
          });
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setCapacityResult({ connectionId: connection.id, windows: [], error: true });
        }
      });
    return () => controller.abort();
  }, [expanded, connection.id]);

  const capacityNumberFmt = useMemo(
    () => new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }),
    [locale]
  );

  const rows = useMemo<QuotaRowLike[]>(() => {
    if (!cache) return [];
    let parsed = parseQuotaData(
      providerKey,
      showSubscriptionDetails ? { ...cache, message: null } : cache
    ) as QuotaRowLike[];
    // Claude's shared parser intentionally omits unreported fractions. Keep
    // those named windows here as unknown, never as zero or an available quota.
    if (providerKey === "claude") {
      const reported = { ...cache.quotas, ...cache.modelQuotas };
      for (const [name, value] of Object.entries(reported)) {
        if (!value || typeof value !== "object" || parsed.some((row) => row.name === name))
          continue;
        const quota = value as Record<string, unknown>;
        if (quota.fractionReported !== false) continue;
        parsed.push({
          name,
          displayName: typeof quota.displayName === "string" ? quota.displayName : undefined,
          resetAt: typeof quota.resetAt === "string" ? quota.resetAt : null,
          fractionReported: false,
        });
      }
    }
    // #6687/#7764: keep the deterministic order of providers that have one
    // (codex/GLM/kimi); everyone else falls into session→weekly→monthly.
    if (!hasFixedQuotaOrder(providerKey) && hasCanonicalWindowOrder(parsed)) {
      parsed = sortQuotasByWindow(parsed);
    }
    return parsed;
  }, [cache, providerKey, showSubscriptionDetails]);

  const messageRow = rows.length === 1 && rows[0].message ? rows[0] : null;
  // Failed fetches keep the previous cache but leave `message` set; surface it
  // whenever it exists, not only when no rows parsed (claude folds it into an
  // error row, others — e.g. xiaomi-mimo's self-tracked fallback — keep it
  // beside real quota rows, where hiding it makes local counters look live).
  const messageText = messageRow?.message ?? (cache?.message ? cache.message : null);
  const creditRows = rows.filter(
    (r) =>
      (r.isCredits || r.isResetCredits) && !(providerKey === "claude" && r.name === "extra_usage")
  );
  const windowRows = rows.filter(
    (r) => !r.isCredits && !r.isResetCredits && quotaWindowRank(r.name) !== null && !r.message
  );
  const otherRows = rows.filter(
    (r) => !r.isCredits && !r.isResetCredits && quotaWindowRank(r.name) === null && r !== messageRow
  );
  const subscriptionRows = rows.filter(
    (row) =>
      !row.isCredits &&
      !row.isResetCredits &&
      !row.isExtraUsageLimit &&
      !row.message &&
      !["credits", "banked_reset_credits", "extra_usage"].includes(row.name)
  );
  const staleSnapshot = Boolean(cache?.stale || cache?._stale || messageText);
  // Other providers retain their compact most-consumed model chips.
  const worstOtherRows = [...otherRows]
    .filter((r) => !r.unlimited)
    .sort((a, b) => remainingOf(a) - remainingOf(b))
    .slice(0, windowRows.length === 0 ? MAX_MODEL_CHIPS_ALONE : MAX_MODEL_CHIPS_WITH_WINDOWS);
  const otherCount = Math.max(0, otherRows.length - worstOtherRows.length);

  const numberFmt = useMemo(() => {
    const ordinary = new Intl.NumberFormat(locale, { maximumFractionDigits: 2 });
    const tiny = new Intl.NumberFormat(locale, {
      notation: "scientific",
      maximumSignificantDigits: 8,
    });
    return {
      format: (value: number) =>
        (value !== 0 && Math.abs(value) < 0.01 ? tiny : ordinary).format(value),
    };
  }, [locale]);
  const observedAt = cache?.observedAt;
  const updatedAgo = formatAgo(
    cache?.fetchedAt ||
      (typeof observedAt === "number" && Number.isFinite(observedAt)
        ? new Date(observedAt).toISOString()
        : cache?._staleSince),
    locale
  );
  const resetDateFmt = useMemo(
    () =>
      new Intl.DateTimeFormat(locale, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZoneName: "short",
      }),
    [locale]
  );
  const percentFmt = useMemo(
    () =>
      new Intl.NumberFormat(locale, {
        style: "percent",
        maximumFractionDigits: 1,
      }),
    [locale]
  );
  const hasDetails = rows.length > 0 && !messageRow;

  function renderUsedText(row: QuotaRowLike): string {
    if (row.isExtraUsageLimit) {
      const value = row.creditCount ?? row.remaining;
      if (typeof value !== "number" || !Number.isFinite(value))
        return t("subscriptionCreditUnknown");
      return row.currency
        ? `${numberFmt.format(value)} ${row.currency}`
        : t("quotaCredits", { count: numberFmt.format(value) });
    }
    if (row.isCredits || row.isResetCredits) {
      return t("quotaCredits", {
        count: numberFmt.format(Number(row.creditCount ?? row.remaining ?? 0)),
      });
    }
    if (row.unlimited) return t("quotaUnlimited");
    const pct = usedPercent(row);
    if (row.isPercentageOnly || !(Number(row.total || 0) > 0)) {
      return pct === null ? "—" : `${pct}%`;
    }
    return `${numberFmt.format(Number(row.used || 0))}/${numberFmt.format(
      Number(row.total || 0)
    )} (${pct === null ? "—" : `${pct}%`})`;
  }

  function chipTitle(row: QuotaRowLike): string {
    const label = row.displayName || row.name;
    const countdown = formatCountdown(row.resetAt);
    return countdown ? `${label} — ${t("quotaResetIn", { time: countdown })}` : label;
  }

  function subscriptionLabel(row: QuotaRowLike): string {
    const label = row.displayName || row.name;
    if (/^session(?:\s*\(5h\))?$/i.test(label)) return t("quotaWindowSession");
    if (/^weekly(?:\s*\(7d\))?$/i.test(label)) return t("quotaWindowWeekly");
    if (/spark/.test(row.name) && /session|weekly/.test(row.name)) {
      const window = /session/.test(row.name) ? t("quotaWindowSession") : t("quotaWindowWeekly");
      const model = (row.displayName || formatQuotaLabel(row.name)).replace(
        /\s+(session|weekly)$/i,
        ""
      );
      return `${model} · ${window}`;
    }
    // Do not collapse scoped weekly/model labels to an ambiguous second "7d".
    return label.replaceAll("_", " ");
  }

  function subscriptionRemaining(row: QuotaRowLike): number | null {
    if (staleSnapshot || row.staleAfterReset || row.fractionReported === false) return null;
    const resetTime = row.resetAt ? Date.parse(row.resetAt) : NaN;
    if (Number.isFinite(resetTime) && resetTime <= clock) return null;
    const value = cache?.modelQuotas?.[row.name] ?? cache?.quotas?.[row.name];
    if (!value || typeof value !== "object") return null;
    const raw = value as Record<string, unknown>;
    if (raw.fractionReported === false) return null;
    const finite = (number: unknown): number is number =>
      typeof number === "number" && Number.isFinite(number);
    if (finite(raw.remainingPercentage)) return Math.min(100, Math.max(0, raw.remainingPercentage));
    if (!finite(raw.total) || raw.total <= 0) return null;
    const remaining = finite(raw.used)
      ? 100 * (1 - raw.used / raw.total)
      : finite(raw.remaining)
        ? (100 * raw.remaining) / raw.total
        : null;
    return remaining === null ? null : Math.min(100, Math.max(0, remaining));
  }

  const refreshControl = onRefresh ? (
    <button
      type="button"
      onClick={onRefresh}
      disabled={refreshing}
      className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[11px] font-medium text-text-muted hover:text-primary disabled:opacity-50 disabled:cursor-not-allowed"
      title={t("quotaRefresh")}
      aria-label={t("quotaRefresh")}
    >
      <span className={`material-symbols-outlined text-[13px] ${refreshing ? "animate-spin" : ""}`}>
        refresh
      </span>
    </button>
  ) : null;
  const subscriptionSection = showSubscriptionDetails ? (
    <section
      aria-labelledby={`${sectionId}-subscription`}
      aria-busy={Boolean(refreshing)}
      className="min-w-0 rounded-lg border border-border/50 bg-surface-secondary/30 p-2.5 text-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 id={`${sectionId}-subscription`} className="font-medium">
          {t("subscriptionQuotaTitle")}
        </h4>
        {refreshControl}
      </div>
      <p className="mt-1 text-text-muted">{t("subscriptionQuotaDescription")}</p>
      {!cache ? <p className="mt-2 text-text-muted">{t("quotaNoData")}</p> : null}
      <p className="mt-1 text-[11px] text-text-muted">
        {updatedAgo
          ? t("subscriptionQuotaUpdated", { time: updatedAgo })
          : t("subscriptionQuotaUpdatedUnknown")}
      </p>
      {staleSnapshot ? <p className="mt-2 text-amber-500">{t("subscriptionQuotaStale")}</p> : null}
      {messageText ? <p className="mt-1 break-words text-amber-500">{messageText}</p> : null}
      <div className="mt-2 grid min-w-0 gap-3">
        {subscriptionRows.length === 0 ? (
          <p className="text-text-muted">{t("subscriptionQuotaUnknown")}</p>
        ) : null}
        {subscriptionRows.map((row) => {
          const label = subscriptionLabel(row);
          const remaining = subscriptionRemaining(row);
          const colors = remaining === null ? null : getBarColor(remaining);
          const resetTime = row.resetAt ? Date.parse(row.resetAt) : NaN;
          const resetKnown = Number.isFinite(resetTime);
          const resetElapsed = resetKnown && resetTime <= clock;
          const usedText = t("subscriptionQuotaUsed", {
            value: remaining === null ? "—" : percentFmt.format((100 - remaining) / 100),
          });
          const remainingText = t("subscriptionQuotaRemaining", {
            value: remaining === null ? "—" : percentFmt.format(remaining / 100),
          });
          const stateText =
            resetElapsed || row.staleAfterReset
              ? t("subscriptionQuotaResetElapsed")
              : staleSnapshot
                ? t("subscriptionQuotaStale")
                : t("subscriptionQuotaUnknown");
          return (
            <div key={row.name} className="min-w-0">
              <div className="flex min-w-0 flex-wrap items-start justify-between gap-x-3 gap-y-1">
                <span className="min-w-0 break-words font-medium [overflow-wrap:anywhere]">
                  {label}
                </span>
                <span className="flex flex-wrap gap-x-3 tabular-nums text-text-muted">
                  <span>{usedText}</span>
                  <span>{remainingText}</span>
                </span>
              </div>
              <div
                role="progressbar"
                aria-label={label}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={remaining ?? undefined}
                aria-valuetext={remaining === null ? stateText : `${usedText}; ${remainingText}`}
                className="mt-1.5 h-2 overflow-hidden rounded-full bg-black/[0.06] dark:bg-white/[0.06]"
              >
                {remaining !== null ? (
                  <span
                    className="block h-full rounded-full"
                    style={{ width: `${remaining}%`, background: colors?.bar }}
                  />
                ) : null}
              </div>
              {remaining === null ? <p className="mt-1 text-text-muted">{stateText}</p> : null}
              <div className="mt-1 flex flex-col gap-0.5 text-[11px] text-text-muted">
                <span>
                  {resetKnown
                    ? resetElapsed
                      ? t("subscriptionQuotaResetElapsed")
                      : t("quotaResetIn", { time: formatCountdown(row.resetAt) })
                    : t("subscriptionQuotaResetUnknown")}
                </span>
                {resetKnown ? (
                  <time dateTime={new Date(resetTime).toISOString()}>
                    {resetDateFmt.format(resetTime)}
                  </time>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  ) : null;

  // No cached data at all — offer the first fetch inline instead of an empty strip.
  if (!cache) {
    return (
      <div className="mt-2 text-xs text-text-muted">
        {showSubscriptionDetails ? (
          subscriptionSection
        ) : (
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-[13px] opacity-60">data_usage</span>
            <span>{t("quotaNoData")}</span>
            {refreshControl}
          </div>
        )}
        <SubscriptionCreditStatus provider={providerKey} snapshot={cache} />
      </div>
    );
  }

  return (
    <div className="mt-2 min-w-0">
      {subscriptionSection}
      <div
        className={`flex flex-wrap items-center gap-1.5 text-xs ${showSubscriptionDetails ? "mt-2" : ""}`}
      >
        {!showSubscriptionDetails && messageText ? (
          <span className="text-amber-500" title={messageText}>
            {messageText}
          </span>
        ) : (
          (showSubscriptionDetails ? [] : windowRows).map((row) => {
            const pct = usedPercent(row);
            const remaining =
              typeof row.remainingPercentage === "number"
                ? row.remainingPercentage
                : pct === null
                  ? null
                  : 100 - pct;
            const colors = remaining === null ? null : getBarColor(remaining);
            return (
              <span
                key={row.name}
                title={chipTitle(row)}
                className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded font-medium"
                style={
                  colors
                    ? { color: colors.text, background: colors.bg }
                    : { color: "inherit", background: "rgba(120,120,120,0.12)" }
                }
              >
                {shortWindowLabel(
                  row.displayName || row.name,
                  t("quotaWindowMonthly"),
                  t("quotaWindowSpark")
                )}
                {pct === null ? " —" : ` ${pct}%`}
              </span>
            );
          })
        )}
        {creditRows.map((row) => (
          <span
            key={row.name}
            title={chipTitle(row)}
            className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded font-medium text-text-muted"
            style={{ background: "rgba(120,120,120,0.12)" }}
          >
            <span className="material-symbols-outlined text-[13px]">toll</span>
            {renderUsedText(row)}
          </span>
        ))}
        {(showSubscriptionDetails ? [] : worstOtherRows).map((row) => {
          const pct = usedPercent(row);
          const colors = getBarColor(remainingOf(row));
          return (
            <span
              key={row.name}
              title={chipTitle(row)}
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded font-medium"
              style={{ color: colors.text, background: colors.bg }}
            >
              {modelChipLabel(row.displayName || row.name)}
              {pct === null ? " —" : ` ${pct}%`}
            </span>
          );
        })}
        {!showSubscriptionDetails && otherCount > 0 && (
          <span
            className="px-1.5 py-0.5 rounded font-medium text-text-muted"
            style={{ background: "rgba(120,120,120,0.12)" }}
            title={t("quotaMoreWindows", { count: otherCount })}
          >
            +{otherCount}
          </span>
        )}
        {!showSubscriptionDetails ? refreshControl : null}
        {!showSubscriptionDetails && updatedAgo && !refreshing ? (
          <span className="text-[11px] text-text-muted/70">{updatedAgo}</span>
        ) : null}
        {(showSubscriptionDetails || hasDetails) && (
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() => setExpanded((v) => !v)}
            className="inline-flex items-center px-1 py-0.5 rounded text-[11px] font-medium text-text-muted hover:text-primary"
            title={t(showSubscriptionDetails ? "quotaCapacityDetails" : "quotaDetails")}
            aria-label={t(showSubscriptionDetails ? "quotaCapacityDetails" : "quotaDetails")}
          >
            <span className="material-symbols-outlined text-[14px]">
              {expanded ? "expand_less" : "expand_more"}
            </span>
            {showSubscriptionDetails ? t("quotaCapacityDetails") : null}
          </button>
        )}
      </div>
      <SubscriptionCreditStatus provider={providerKey} snapshot={cache} />

      {expanded && (showSubscriptionDetails || hasDetails) ? (
        <div className="mt-2 rounded-lg border border-border/50 bg-surface-secondary/30 p-2.5 grid gap-1.5">
          {(showSubscriptionDetails ? [] : rows).map((row) => {
            if (row.message) return null;
            const pct = usedPercent(row);
            const remaining =
              typeof row.remainingPercentage === "number"
                ? row.remainingPercentage
                : pct === null
                  ? null
                  : 100 - pct;
            const colors = remaining === null ? null : getBarColor(remaining);
            const countdown = formatCountdown(row.resetAt);
            const isCreditRow = row.isCredits || row.isResetCredits;
            return (
              <div key={row.name} className="flex items-center gap-2 text-xs">
                <span
                  className="w-40 shrink-0 truncate font-medium"
                  title={
                    row.isExtraUsageLimit
                      ? t("subscriptionCreditRemainingLimit")
                      : row.displayName || row.name
                  }
                >
                  {row.isExtraUsageLimit
                    ? t("subscriptionCreditRemainingLimit")
                    : row.displayName || row.name}
                </span>
                {isCreditRow || remaining === null ? (
                  <span className="flex-1" />
                ) : (
                  <span className="h-1.5 flex-1 rounded-full overflow-hidden bg-black/[0.06] dark:bg-white/[0.06]">
                    <span
                      className="block h-full rounded-full"
                      style={{
                        width: `${Math.min(100, Math.max(0, remaining))}%`,
                        background: colors?.bar,
                      }}
                    />
                  </span>
                )}
                <span
                  className="w-44 shrink-0 text-right tabular-nums text-text-muted"
                  style={colors && !isCreditRow ? { color: colors.text } : undefined}
                >
                  {renderUsedText(row)}
                  {countdown ? (
                    <span className="block text-[10px] opacity-70">
                      {t("quotaResetIn", { time: countdown })}
                    </span>
                  ) : null}
                </span>
              </div>
            );
          })}
          <div
            className={`${showSubscriptionDetails ? "" : "mt-1 border-t border-border/50 pt-2 "}text-xs`}
          >
            <div className="mb-1 font-medium">{t("quotaCapacityTitle")}</div>
            <p className="mb-2 text-text-muted">{t("quotaCapacityDescription")}</p>
            {capacityLoading ? (
              <p className="text-text-muted">{t("quotaCapacityLoading")}</p>
            ) : null}
            {!capacityLoading && capacity?.length === 0 ? (
              <p className="text-text-muted">
                {t(capacityError ? "quotaCapacityUnavailable" : "quotaCapacityUnknown")}
              </p>
            ) : null}
            {capacity?.map((estimate) => (
              <div key={estimate.windowKey} className="flex flex-wrap justify-between gap-x-3 py-1">
                <span
                  className="min-w-0 break-words [overflow-wrap:anywhere]"
                  title={estimate.windowKey}
                >
                  {estimate.windowKey}
                </span>
                {estimate.status === "estimated" ? (
                  <span className="text-right tabular-nums text-text-muted">
                    {t(
                      estimate.estimateKind === "recorded_lower_bound"
                        ? "quotaCapacityLowerBound"
                        : "quotaCapacityEstimate"
                    )}
                    :{" "}
                    {t("quotaCapacityTotal", {
                      count:
                        estimate.estimatedTotalTokens === null
                          ? t("quotaCapacityUnknownValue")
                          : capacityNumberFmt.format(estimate.estimatedTotalTokens),
                    })}
                    {estimate.remainingTokens === null
                      ? ""
                      : ` · ${t("quotaCapacityRemaining", { count: capacityNumberFmt.format(estimate.remainingTokens) })}`}
                    {estimate.totalTokensHigh === null
                      ? ` · ${t("quotaCapacityUpperUnknown")}`
                      : ` · ${t("quotaCapacityRange", { low: estimate.totalTokensLow === null ? t("quotaCapacityUnknownValue") : capacityNumberFmt.format(estimate.totalTokensLow), high: capacityNumberFmt.format(estimate.totalTokensHigh) })}`}
                  </span>
                ) : (
                  <span className="text-right text-text-muted">
                    {t("quotaCapacityUnknownValue")} · {estimate.reason.replaceAll("_", " ")}
                  </span>
                )}
              </div>
            ))}
            {capacity?.some((estimate) => estimate.caveats.length > 0) ? (
              <details className="mt-1 text-text-muted">
                <summary className="cursor-pointer">{t("quotaCapacityCaveats")}</summary>
                <ul className="list-disc pl-5 pt-1">
                  {capacity[0]?.caveats.map((caveat) => (
                    <li key={caveat}>{caveat}</li>
                  ))}
                </ul>
              </details>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
