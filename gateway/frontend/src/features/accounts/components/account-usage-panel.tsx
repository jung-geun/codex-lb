import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { Clock, Flame, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { AccountTrendChartProps } from "@/features/accounts/components/account-trend-chart";
import type { AccountTrendSeries } from "@/features/accounts/account-trend-data";
import type {
  AccountTrendsResponse,
  AccountUsageResetCredits,
} from "@/features/accounts/schemas";
import type { AccountView } from "@/features/accounts/source-selection";
import type { ModelSourceApiUsage, ModelSourceQuotaHistoryResponse, ModelSourceUsageSummary } from "@/features/model-sources/schemas";
import { sourceWindowIsStale, sourceWindowLabel } from "@/features/model-sources/monitoring";
import { QuotaEstimates } from "@/features/quota-estimates/quota-estimates";
import { useChartColors } from "@/hooks/use-chart-colors";
import { useDateDisplayFormatStore } from "@/hooks/use-date-format";
import { useSmoothPercent } from "@/hooks/use-smooth-percent";
import { useCurrentTime } from "@/hooks/use-current-time";
import { quotaBarColor, quotaBarTrack } from "@/utils/account-status";
import {
  formatCompactNumber,
  formatCurrency,
  formatDateTimeInline,
  formatPercentNullable,
  formatQuotaResetLabel,
  formatResetRelative,
  formatSingleUnitRemaining,
  formatWindowLabel,
} from "@/utils/formatters";

const AccountTrendChart = lazy(() =>
  import("@/features/accounts/components/account-trend-chart").then((module) => ({
    default: (props: AccountTrendChartProps) => <module.AccountTrendChart {...props} />,
  })),
);

export type AccountUsagePanelProps = {
  account: AccountView;
  trends?: AccountTrendsResponse | null;
  sourceUsage?: ModelSourceUsageSummary | null;
  sourceHistory?: ModelSourceQuotaHistoryResponse | null;
  sourceUsageLoading?: boolean;
  sourceUsageError?: boolean;
  sourceUsageDays?: 1 | 7 | 30;
  onSourceUsageDaysChange?: (days: 1 | 7 | 30) => void;
  apiUsage?: ModelSourceApiUsage | null;
  apiUsageLoading?: boolean;
  apiUsageError?: boolean;
  sourceHistoryLoading?: boolean;
  sourceHistoryError?: boolean;
  resetCredits?: AccountUsageResetCredits | null;
  resetCreditsLoading?: boolean;
  resetCreditsUnavailable?: boolean;
  resetDisabled?: boolean;
  onReset?: (accountId: string) => void;
};

function QuotaRow({
  label,
  percent,
  resetAt,
}: {
  label: string;
  percent: number | null;
  resetAt: string | null | undefined;
}) {
  const { t } = useTranslation();
  const clamped = percent === null ? 0 : Math.max(0, Math.min(100, percent));
  const hasPercent = percent !== null;
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-xs">
        <span className="font-medium">{t("accounts.usage.remainingLabel", { label })}</span>
        <span
          className={cn(
            "tabular-nums font-medium",
            !hasPercent
              ? "text-muted-foreground"
              : clamped >= 70
                ? "text-emerald-600 dark:text-emerald-400"
                : clamped >= 30
                  ? "text-amber-600 dark:text-amber-400"
                  : "text-red-600 dark:text-red-400",
          )}
        >
          {formatPercentNullable(percent, 1)}
        </span>
      </div>
      {hasPercent ? <progress className="sr-only" max={100} value={clamped} aria-label={t("accounts.listItem.quotaRemainingAria", { label })} /> : null}
      <div className={cn("h-1.5 w-full overflow-hidden rounded-full", hasPercent ? quotaBarTrack(clamped) : "bg-muted")}>
        <div
          className={cn("h-full rounded-full transition-colors duration-500 ease-out", quotaBarColor(clamped))}
          style={{ width: `${clamped}%` }}
        />
      </div>
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Clock className="h-3 w-3 shrink-0" />
        <span>{t("accounts.usage.resetAt", { label: formatQuotaResetLabel(resetAt ?? null) })}</span>
      </div>
    </div>
  );
}

const ADDITIONAL_LIMIT_LABELS: Record<string, string> = {
  codex_spark: "GPT-5.3-Codex-Spark",
  codex_other: "GPT-5.3-Codex-Spark",
  "gpt-5.3-codex-spark": "GPT-5.3-Codex-Spark",
};

function formatAdditionalLimitName(limitName: string, quotaKey?: string | null): string {
  const normalizedQuotaKey = quotaKey?.trim().toLowerCase();
  if (normalizedQuotaKey && ADDITIONAL_LIMIT_LABELS[normalizedQuotaKey]) {
    return ADDITIONAL_LIMIT_LABELS[normalizedQuotaKey];
  }
  const normalized = limitName.trim().toLowerCase();
  return ADDITIONAL_LIMIT_LABELS[normalized] ?? limitName.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatResetCountdown(resetAt: number | null): string | null {
  if (resetAt === null) return null;
  const diffMs = resetAt * 1000 - Date.now();
  if (diffMs <= 0) return "resetting";
  return formatResetRelative(diffMs);
}

function AdditionalQuotaRow({
  label,
  usedPercent,
  resetAt,
}: {
  label: string;
  usedPercent: number | null;
  resetAt: number | null;
}) {
  const { t } = useTranslation();
  const clamped = usedPercent === null ? 0 : Math.max(0, Math.min(100, usedPercent));
  const countdown = formatResetCountdown(resetAt);
  const countdownLabel = countdown === "resetting"
    ? t("formatters.resetting")
    : countdown
      ? t("accounts.usage.resetAt", { label: countdown })
      : null;

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="tabular-nums font-medium">{usedPercent === null ? t("sourceMonitoring.unknown") : t("accounts.usage.percentUsed", { percent: Math.round(usedPercent) })}</span>
      </div>
      <div className="h-1.5 rounded-full bg-muted">
        <div
          className={cn(
            "h-full rounded-full transition-all",
            clamped > 95
              ? "bg-red-500"
              : clamped > 80
                ? "bg-orange-500"
                : clamped > 60
                  ? "bg-amber-500"
                  : "bg-green-500",
          )}
          style={{ width: `${clamped}%` }}
        />
      </div>
      {countdownLabel ? <p className="text-[11px] text-muted-foreground">{countdownLabel}</p> : null}
    </div>
  );
}

const ADDITIONAL_ROUTING_POLICY_LABELS: Record<string, string> = {
  burn_first: "Burn first",
  normal: "Normal",
  preserve: "Preserve",
};

function ResetCreditsRow({
  accountId,
  resetCredits,
  loading,
  unavailable,
  nearestExpiresAt,
  resetDisabled,
  onReset,
}: {
  accountId: string;
  resetCredits?: AccountUsageResetCredits | null;
  loading?: boolean;
  unavailable?: boolean;
  nearestExpiresAt?: string | null;
  resetDisabled?: boolean;
  onReset?: (accountId: string) => void;
}) {
  const { t } = useTranslation();
  const dateDisplayFormat = useDateDisplayFormatStore((state) => state.dateDisplayFormat);
  if (resetCredits == null && !loading && !unavailable) {
    return null;
  }

  const availableCount = resetCredits?.availableCount ?? 0;
  const valueLabel =
    loading && resetCredits == null
      ? t("accounts.usage.resetCredits.checking")
      : unavailable && resetCredits == null
        ? t("common.states.unavailable")
        : t("accounts.usage.resetCredits.available", { count: availableCount });
  const expiryCountdown = nearestExpiresAt ? formatSingleUnitRemaining(nearestExpiresAt) : null;

  return (
    <div className="flex items-center justify-between gap-3 rounded-md border bg-background/60 px-3 py-2 text-xs">
      <span className="flex min-w-0 items-center gap-2 text-muted-foreground">
        <RotateCcw className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span className="truncate font-medium">{t("accounts.usage.resetCredits.title")}</span>
      </span>
      <span className="flex min-w-0 flex-1 items-center justify-end gap-2">
        {nearestExpiresAt && expiryCountdown ? (
          <span className="min-w-0 max-w-[min(18rem,42vw)] truncate text-[11px] text-muted-foreground">
            {t("accounts.usage.resetCredits.expires", {
              time: formatDateTimeInline(nearestExpiresAt, dateDisplayFormat),
            })}{" "}
            <span className="tabular-nums">({expiryCountdown.label})</span>
          </span>
        ) : null}
        <span className="shrink-0 tabular-nums font-semibold">{valueLabel}</span>
        {onReset ? (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-6 shrink-0 gap-1 px-1.5 text-[11px]"
            aria-label={t("accounts.usageResetDialog.title")}
            onClick={() => onReset(accountId)}
            disabled={resetDisabled || availableCount <= 0}
          >
            <RotateCcw className="h-3 w-3" aria-hidden="true" />
            {t("common.actions.reset")}
          </Button>
        ) : null}
      </span>
    </div>
  );
}

export function AccountUsagePanel({
  account,
  trends,
  sourceUsage,
  sourceHistory,
  sourceUsageLoading = false,
  sourceUsageError = false,
  sourceUsageDays,
  onSourceUsageDaysChange,
  apiUsage,
  apiUsageLoading = false,
  apiUsageError = false,
  sourceHistoryLoading = false,
  sourceHistoryError = false,
  resetCredits,
  resetCreditsLoading,
  resetCreditsUnavailable,
  resetDisabled = false,
  onReset,
}: AccountUsagePanelProps) {
  const { t } = useTranslation();
  const periodicNow = useCurrentTime();
  const [expiryNow, setExpiryNow] = useState(0);
  const now = Math.max(periodicNow, expiryNow);
  useEffect(() => {
    if (!apiUsage) return;
    let nextDeadline = Infinity;
    const consider = (deadline: number) => {
      if (deadline > now && deadline < nextDeadline) nextDeadline = deadline;
    };
    for (const rate of apiUsage.rateLimits) {
      const staleAt = Date.parse(rate.observedAt) + 300_001;
      consider(Math.min(staleAt, rate.tokensResetAt ? Date.parse(rate.tokensResetAt) : Infinity));
      consider(Math.min(staleAt, rate.requestsResetAt ? Date.parse(rate.requestsResetAt) : Infinity));
    }
    if (apiUsage.balanceObservedAt) consider(Date.parse(apiUsage.balanceObservedAt) + 300_001);
    if (!Number.isFinite(nextDeadline)) return;
    const timer = window.setTimeout(() => setExpiryNow(Date.now()), Math.max(0, nextDeadline - Date.now()));
    return () => window.clearTimeout(timer);
  }, [apiUsage, now]);
  const chartColors = useChartColors();
  const source = account.source;
  const meteredApi = source?.billingMode === "api_metered";
  const balanceStale = apiUsage?.balanceObservedAt ? sourceWindowIsStale({ observedAt: apiUsage.balanceObservedAt, resetsAt: null }, now) : false;
  const sourceTrends = useMemo(() => {
    const series = new Map<string, AccountTrendSeries>();
    for (const window of sourceHistory?.windows ?? []) {
      const key = JSON.stringify([window.windowKey, window.model]);
      let entry = series.get(key);
      if (!entry) {
        entry = { key, label: sourceWindowLabel(window, t), points: [] };
        series.set(key, entry);
      }
      entry.points.push({ t: window.observedAt, v: window.usedRatio === null ? null : (1 - window.usedRatio) * 100 });
    }
    return {
      primary: series.get(JSON.stringify(["five_hour", null]))?.points ?? [],
      secondary: series.get(JSON.stringify(["seven_day", null]))?.points ?? [],
      additional: [...series.values()].filter((entry) => entry.key !== JSON.stringify(["five_hour", null]) && entry.key !== JSON.stringify(["seven_day", null])),
    };
  }, [sourceHistory, t]);
  const additionalWindows = !meteredApi ? source?.quotaWindows?.filter((window) => !((window.windowKey === "five_hour" || window.windowKey === "seven_day") && window.model === null)) ?? [] : [];
  const primaryState = useSmoothPercent(account.usage?.primaryRemainingPercent ?? null);
  const secondaryState = useSmoothPercent(account.usage?.secondaryRemainingPercent ?? null);
  const monthlyState = useSmoothPercent(account.usage?.monthlyRemainingPercent ?? null);
  const primary = primaryState.percent;
  const secondary = secondaryState.percent;
  const monthly = monthlyState.percent;
  const requestUsage = account.requestUsage ?? null;
  const hasRequestUsage = (requestUsage?.requestCount ?? 0) > 0;
  const hasPrimaryWindow = account.windowMinutesPrimary != null || primaryState.everKnown;
  const hasSecondaryWindow = account.windowMinutesSecondary != null || secondaryState.everKnown;
  const hasMonthlyWindow = account.windowMinutesMonthly != null || monthlyState.everKnown;
  const weeklyOnly = !hasPrimaryWindow && hasSecondaryWindow;
  const primaryTrendPoints = source ? sourceTrends.primary : trends?.primary ?? [];
  const secondaryTrendPoints = source ? sourceTrends.secondary : trends?.secondary ?? [];
  const secondaryScheduledTrendPoints = source ? [] : trends?.secondaryScheduled ?? [];
  const monthlyOnly = hasMonthlyWindow && !hasPrimaryWindow && !hasSecondaryWindow;
  const hasTrends = !meteredApi && (source?.authType === "claude_oauth" || primaryTrendPoints.length > 0 || secondaryTrendPoints.length > 0 || secondaryScheduledTrendPoints.length > 0);
  const sourceUsageTitle = sourceUsageDays && sourceUsageDays !== 1 ? t("accounts.sourceUsage.titleDays", { days: sourceUsageDays }) : t("accounts.sourceUsage.title");

  return (
    <div className="min-w-0 space-y-4 rounded-lg border bg-muted/30 p-4">
      <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">{t("accounts.usage.title")}</h3>
      {!meteredApi && (!source || hasPrimaryWindow || hasSecondaryWindow) ? <div className={cn("grid gap-4", weeklyOnly || monthlyOnly ? "grid-cols-1" : "grid-cols-1 sm:grid-cols-2")}>
        {monthlyOnly ? (
          <QuotaRow label={t("common.quota.monthly")} percent={monthly} resetAt={account.resetAtMonthly} />
        ) : (
          <>
            {!weeklyOnly && <QuotaRow label="5h" percent={primary} resetAt={account.resetAtPrimary} />}
            <QuotaRow label={t("common.quota.weekly")} percent={secondary} resetAt={account.resetAtSecondary} />
          </>
        )}
      </div> : !meteredApi && !source?.quotaWindows?.length ? <p className="text-xs text-muted-foreground">{t("modelSources.detail.providerBalanceUnknown")}</p> : null}
      {source?.quotaError ? <p role="status" className="text-xs text-destructive">{t("sourceMonitoring.observationFailed")}</p> : null}
      {!meteredApi && source?.quotaWindows?.some((window) => sourceWindowIsStale(window, now)) ? <p role="status" className="text-xs text-amber-600 dark:text-amber-400">{t("sourceMonitoring.status.stale")}</p> : null}
      {source?.quotaCheckedAt ? <p className="text-[11px] text-muted-foreground">{t(source.billingMode === "api_metered" ? "modelSources.detail.apiCatalogChecked" : "modelSources.subscription.quotaChecked", { date: formatDateTimeInline(source.quotaCheckedAt) })}</p> : null}
      {meteredApi ? <div role="region" aria-label={t("accounts.apiUsage.title")} className="space-y-2 rounded-md border bg-background/60 px-3 py-2 text-xs">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">{t("accounts.apiUsage.rateWindow")}</p>
        <p className="text-[11px] text-muted-foreground">{t("accounts.apiUsage.rateDescription")}</p>
        {apiUsageLoading ? <p role="status">{t("modelSources.subscription.working")}</p> : apiUsageError ? <p role="alert" className="text-destructive">{t("accounts.apiUsage.failed")}</p> : apiUsage ? <>
          {apiUsage.rateLimits.length === 0 ? <p>{t("accounts.apiUsage.noRates")}</p> : null}
          {apiUsage.rateLimits.map((rate) => {
            const tokensStale = sourceWindowIsStale({ observedAt: rate.observedAt, resetsAt: rate.tokensResetAt }, now);
            const requestsStale = sourceWindowIsStale({ observedAt: rate.observedAt, resetsAt: rate.requestsResetAt }, now);
            return <div key={JSON.stringify([rate.model, rate.tokenKind])} className="space-y-1 border-t pt-2">
              {rate.model ? <p className="break-all font-medium">{rate.model}</p> : null}
              <dl className="grid gap-2 sm:grid-cols-2">
                {rate.tokenLimit !== null || rate.tokensRemaining !== null || rate.tokensResetAt !== null ? <div>
                  <dt>{t(`accounts.apiUsage.${rate.tokenKind}`)}</dt>
                  <dd className="tabular-nums font-medium">{t("accounts.apiUsage.remaining", { remaining: !tokensStale && rate.tokensRemaining !== null ? rate.tokensRemaining.toLocaleString() : t("sourceMonitoring.unknown"), limit: rate.tokenLimit?.toLocaleString() ?? t("sourceMonitoring.unknown") })}</dd>
                  <p className="text-[11px] text-muted-foreground">{t("accounts.apiUsage.reset", { date: rate.tokensResetAt ? formatDateTimeInline(rate.tokensResetAt) : t("sourceMonitoring.unknown") })}</p>
                  {tokensStale ? <p role="status" className="text-amber-600 dark:text-amber-400">{t("accounts.apiUsage.stale")}</p> : null}
                </div> : null}
                {rate.requestLimit !== null || rate.requestsRemaining !== null || rate.requestsResetAt !== null ? <div>
                  <dt>{t("accounts.apiUsage.requests")}</dt>
                  <dd className="tabular-nums font-medium">{t("accounts.apiUsage.remaining", { remaining: !requestsStale && rate.requestsRemaining !== null ? rate.requestsRemaining.toLocaleString() : t("sourceMonitoring.unknown"), limit: rate.requestLimit?.toLocaleString() ?? t("sourceMonitoring.unknown") })}</dd>
                  <p className="text-[11px] text-muted-foreground">{t("accounts.apiUsage.reset", { date: rate.requestsResetAt ? formatDateTimeInline(rate.requestsResetAt) : t("sourceMonitoring.unknown") })}</p>
                  {requestsStale ? <p role="status" className="text-amber-600 dark:text-amber-400">{t("accounts.apiUsage.stale")}</p> : null}
                </div> : null}
              </dl>
              <p className="text-[11px] text-muted-foreground">{t("accounts.apiUsage.observed", { date: formatDateTimeInline(rate.observedAt) })}</p>
            </div>;
          })}
          <p className="border-t pt-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">{t("accounts.apiUsage.balance")}</p>
          {apiUsage.balanceStatus === "available" && !apiUsage.balanceError && !balanceStale && apiUsage.balanceObservedAt && apiUsage.balances.length > 0 ? <>
            {apiUsage.balances.map((balance) => <p key={balance.currency} className="tabular-nums font-medium">{balance.totalBalance} {balance.currency}</p>)}
            {apiUsage.balanceIsAvailable === false ? <p role="status" className="text-destructive">{t("accounts.apiUsage.balanceInsufficient")}</p> : null}
          </> : <p role="status">{t(apiUsage.balanceStatus === "unavailable" ? "accounts.apiUsage.balanceUnavailable" : balanceStale ? "accounts.apiUsage.balanceStale" : "accounts.apiUsage.balanceUnknown")}</p>}
          {apiUsage.balanceObservedAt ? <p className="text-[11px] text-muted-foreground">{t("accounts.apiUsage.observed", { date: formatDateTimeInline(apiUsage.balanceObservedAt) })}</p> : null}
        </> : <p>{t("accounts.apiUsage.noRates")}</p>}
      </div> : null}
      <ResetCreditsRow
        accountId={account.accountId}
        resetCredits={resetCredits}
        loading={resetCreditsLoading}
        unavailable={resetCreditsUnavailable}
        nearestExpiresAt={account.resetCreditNearestExpiresAt ?? null}
        resetDisabled={resetDisabled}
        onReset={onReset}
      />
      <div role="region" aria-label={source ? sourceUsageTitle : t("accounts.usage.requestLogsTotal")} className="rounded-md border bg-background/60 px-3 py-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">{source ? sourceUsageTitle : t("accounts.usage.requestLogsTotal")}</p>
          {sourceUsageDays && onSourceUsageDaysChange ? <Select value={String(sourceUsageDays)} onValueChange={(value) => { if (value === "1" || value === "7" || value === "30") onSourceUsageDaysChange(Number(value) as 1 | 7 | 30); }}>
            <SelectTrigger size="sm" className="w-auto" aria-label={t("accounts.sourceUsage.period")}><SelectValue /></SelectTrigger>
            <SelectContent>{([1, 7, 30] as const).map((days) => <SelectItem key={days} value={String(days)}>{t("accounts.sourceUsage.days", { count: days })}</SelectItem>)}</SelectContent>
          </Select> : null}
        </div>
        {source ? (
          <>
            {sourceUsageLoading ? <p role="status" className="mt-1 text-xs text-muted-foreground">{t("modelSources.subscription.working")}</p> : null}
            {sourceUsageError ? <p role="alert" className="mt-1 text-xs text-destructive">{t("sourceMonitoring.usageFailed")}</p> : null}
            {sourceUsage && !sourceUsageError && !sourceUsageLoading ? <>
              <p className="mt-1 break-words text-xs tabular-nums text-muted-foreground">
                {t("accounts.usage.requestUsageMeta", {
                  tokens: (sourceUsage.inputTokens === null || sourceUsage.outputTokens === null)
                    && (sourceUsage.inputTokens ?? 0) + (sourceUsage.outputTokens ?? 0) === 0
                    ? t("sourceMonitoring.unknown")
                    : formatCompactNumber((sourceUsage.inputTokens ?? 0) + (sourceUsage.outputTokens ?? 0)),
                  cached: sourceUsage.cachedInputTokens === null ? t("sourceMonitoring.unknown") : formatCompactNumber(sourceUsage.cachedInputTokens),
                  requests: formatCompactNumber(sourceUsage.requestCount),
                  cost: sourceUsage.costUsd === null ? t("sourceMonitoring.unknown") : formatCurrency(sourceUsage.costUsd),
                })}
              </p>
              <p className="mt-1 text-[11px] text-muted-foreground">{formatDateTimeInline(sourceUsage.windowStart)} – {formatDateTimeInline(sourceUsage.windowEnd)}</p>
              <p className="mt-1 text-[11px] text-muted-foreground">{t("accounts.sourceUsage.description")}</p>
              {sourceUsage.unknownUsageCount > 0 ? <p role="status" className="mt-1 text-xs text-muted-foreground">{t("accounts.sourceUsage.incompleteUsage", { count: sourceUsage.unknownUsageCount })}</p> : null}
              {sourceUsage.unpricedRequestCount > 0 ? <p role="status" className="mt-1 text-xs text-muted-foreground">{t("accounts.sourceUsage.incompleteCost", { count: sourceUsage.unpricedRequestCount, known: sourceUsage.knownCostUsd === null ? t("sourceMonitoring.unknown") : formatCurrency(sourceUsage.knownCostUsd) })}</p> : null}
              <p className="mt-1 text-[11px] text-muted-foreground">{t("accounts.sourceUsage.lastRequest", { date: sourceUsage.lastRequestAt ? formatDateTimeInline(sourceUsage.lastRequestAt) : t("accounts.sourceUsage.noRequests") })}</p>
              <details className="mt-2 text-xs text-muted-foreground">
                <summary className="cursor-pointer">{t("sourceMonitoring.usageDetails")}</summary>
                <dl className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3">
                  {([["successes", sourceUsage.successCount], ["errors", sourceUsage.errorCount], ["cancelled", sourceUsage.cancelledCount], ["inputTokens", sourceUsage.inputTokens], ["outputTokens", sourceUsage.outputTokens], ["cachedTokens", sourceUsage.cachedInputTokens], ["cacheCreationTokens", sourceUsage.cacheCreationInputTokens], ["reasoningTokens", sourceUsage.reasoningTokens]] as const).map(([key, value]) => <div key={key}><dt>{t(`accounts.sourceUsage.${key}`)}</dt><dd className="font-medium tabular-nums text-foreground">{value === null ? t("sourceMonitoring.unknown") : formatCompactNumber(value)}</dd></div>)}
                </dl>
              </details>
            </> : null}
          </>
        ) : hasRequestUsage ? (
          <p className="mt-1 break-words text-xs tabular-nums text-muted-foreground">
            {t("accounts.usage.requestUsageMeta", {
              tokens: formatCompactNumber(requestUsage?.totalTokens),
              cached: formatCompactNumber(requestUsage?.cachedInputTokens),
              requests: formatCompactNumber(requestUsage?.requestCount),
              cost: formatCurrency(requestUsage?.totalCostUsd),
            })}
          </p>
        ) : (
          <p className="mt-1 text-xs text-muted-foreground">{t("accounts.usage.noRequestUsage")}</p>
        )}
      </div>
      {source?.billingMode !== "api_metered" ? <QuotaEstimates account={account} /> : null}
      {account.additionalQuotas.length > 0 ? (
        <div className="space-y-3">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            {t("accounts.usage.additionalQuotas")}
          </p>
          {account.additionalQuotas.map((quota) => (
            <div key={quota.quotaKey ?? quota.limitName} className="rounded-md border bg-background/60 px-3 py-2 space-y-2">
              <p className="text-xs font-medium">
                <span>{quota.displayLabel ?? formatAdditionalLimitName(quota.limitName, quota.quotaKey)}</span>
                {quota.routingPolicy != null && quota.routingPolicy !== "inherit" ? (
                  <span className="ml-2 inline-flex items-center gap-1 rounded-full border border-orange-200 bg-orange-50 px-1.5 py-0.5 text-[10px] font-medium text-orange-700 dark:border-orange-900/60 dark:bg-orange-950/40 dark:text-orange-300">
                    <Flame className="h-3 w-3" aria-hidden="true" />
                    {t(`common.routingPolicies.${quota.routingPolicy === "burn_first" ? "burnFirst" : quota.routingPolicy}`, {
                      defaultValue: ADDITIONAL_ROUTING_POLICY_LABELS[quota.routingPolicy] ?? quota.routingPolicy,
                    })}
                  </span>
                ) : null}
              </p>
              {quota.primaryWindow != null ? (
                <AdditionalQuotaRow
                  label={formatWindowLabel("primary", quota.primaryWindow.windowMinutes ?? null)}
                  usedPercent={quota.primaryWindow.usedPercent}
                  resetAt={quota.primaryWindow.resetAt ?? null}
                />
              ) : null}
              {quota.secondaryWindow != null ? (
                <AdditionalQuotaRow
                  label={formatWindowLabel("secondary", quota.secondaryWindow.windowMinutes ?? null)}
                  usedPercent={quota.secondaryWindow.usedPercent}
                  resetAt={quota.secondaryWindow.resetAt ?? null}
                />
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {additionalWindows.length > 0 ? <div className="space-y-3">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">{t("accounts.usage.additionalQuotas")}</p>
        {additionalWindows.map((window) => <div key={JSON.stringify([window.windowKey, window.model])} className="space-y-2 rounded-md border bg-background/60 px-3 py-2">
          <p className="text-xs font-medium">{sourceWindowLabel(window, t)}</p>
          <AdditionalQuotaRow label={window.windowKey.startsWith("seven_day") ? t("common.quota.weekly") : window.windowKey === "five_hour" ? "5h" : window.windowKey} usedPercent={window.usedRatio === null ? null : window.usedRatio * 100} resetAt={window.resetsAt ? Date.parse(window.resetsAt) / 1000 : null} />
        </div>)}
      </div> : null}
      {hasTrends && (
        <div className="pt-3">
          <div className="mb-2 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">{t("accounts.usage.trendTitle")}</h4>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-muted-foreground">
              <span className="flex items-center gap-1.5">
                <span className="inline-block h-2 w-2 rounded-full bg-chart-1" />
                5h
              </span>
              <span className="flex items-center gap-1.5">
                <span className="inline-block h-2 w-2 rounded-full bg-chart-2" />
                {monthlyOnly ? t("common.quota.monthly") : t("common.quota.weekly")}
              </span>
              {secondaryScheduledTrendPoints.length > 0 ? (
                <span className="flex items-center gap-1.5">
                  <span className="inline-block h-0 w-4 border-t border-dashed border-chart-2" />
                  {monthlyOnly ? t("accounts.usage.monthlyPlan") : t("accounts.usage.weeklyPlan")}
                </span>
              ) : null}
              {sourceTrends.additional.map((series, index) => <span key={series.key} className="flex items-center gap-1.5"><span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: chartColors[(index + 2) % chartColors.length] }} />{series.label}</span>)}
            </div>
          </div>
          {source ? <p className="mb-2 text-[11px] text-muted-foreground">{t("sourceMonitoring.history.description")}</p> : null}
          {sourceHistoryLoading ? <p role="status" className="text-xs text-muted-foreground">{t("sourceMonitoring.history.loading")}</p> : null}
          {sourceHistoryError ? <p role="alert" className="text-xs text-destructive">{t("sourceMonitoring.history.error")}</p> : null}
          {sourceHistory && !sourceHistoryLoading && !sourceHistoryError && !sourceHistory.windows.some((window) => window.usedRatio !== null) ? <p role="status" className="text-xs text-muted-foreground">{t(sourceHistory.windows.length === 0 ? "sourceMonitoring.history.empty" : "sourceMonitoring.history.noPercentages")}</p> : null}
          {(!source || (!sourceHistoryLoading && !sourceHistoryError && sourceHistory?.windows.some((window) => window.usedRatio !== null))) ? <Suspense fallback={<div className="h-[220px]" />}>
            <AccountTrendChart
              primary={primaryTrendPoints}
              secondary={secondaryTrendPoints}
              secondaryScheduled={secondaryScheduledTrendPoints}
              additional={sourceTrends.additional}
              windowStart={sourceHistory?.windowStart}
              windowEnd={sourceHistory?.windowEnd}
            />
          </Suspense> : null}
          {sourceHistory && sourceHistory.windows.length > 0 ? <details className="mt-2 text-xs text-muted-foreground">
            <summary className="cursor-pointer">{t("sourceMonitoring.history.observations")}</summary>
            <div className="mt-2 max-h-64 overflow-auto">
              <table className="w-full text-left">
                <caption className="sr-only">{t("sourceMonitoring.history.observations")}</caption>
                <thead><tr className="border-b"><th scope="col" className="px-2 py-1.5 font-medium">{t("sourceMonitoring.history.observedAt")}</th><th scope="col" className="px-2 py-1.5 font-medium">{t("sourceMonitoring.history.window")}</th><th scope="col" className="px-2 py-1.5 text-right font-medium">{t("sourceMonitoring.history.remaining")}</th></tr></thead>
                <tbody>{[...sourceHistory.windows].sort((left, right) => Date.parse(left.observedAt) - Date.parse(right.observedAt)).map((window, index) => <tr key={`${window.windowKey}:${window.model}:${window.observedAt}:${index}`} className="border-b last:border-0">
                  <td className="px-2 py-1.5"><time dateTime={window.observedAt}>{formatDateTimeInline(window.observedAt)}</time></td>
                  <td className="px-2 py-1.5">{sourceWindowLabel(window, t)}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums">{window.usedRatio === null ? t("sourceMonitoring.history.unknown") : `${((1 - window.usedRatio) * 100).toFixed(1)}%`}</td>
                </tr>)}</tbody>
              </table>
            </div>
          </details> : null}
        </div>
      )}
    </div>
  );
}
