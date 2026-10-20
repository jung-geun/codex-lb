import { useTranslation } from "react-i18next";

import type { AccountView } from "@/features/accounts/source-selection";
import { sourceWindowLabel } from "@/features/model-sources/monitoring";
import type { QuotaEstimate } from "@/features/quota-estimates/schemas";
import { formatCompactNumber, formatDateTimeInline } from "@/utils/formatters";

export function QuotaEstimates({ account, compact = false, dense = false }: { account: AccountView; compact?: boolean; dense?: boolean }) {
  const { t } = useTranslation();
  const windows = account.quotaEstimates;
  const state = account.quotaEstimateState;
  if (!state) return null;
  const label = (estimate: QuotaEstimate) => {
    const windowKey = estimate.windowKey === "primary" ? "five_hour" : estimate.windowKey === "secondary" ? "seven_day" : estimate.windowKey;
    if (windowKey === "unknown") return t("sourceMonitoring.unknown");
    const additional = account.additionalQuotas.find((quota) => estimate.windowKey.startsWith(`${quota.quotaKey ?? quota.limitName}:`));
    if (additional) return `${additional.displayLabel ?? additional.limitName} · ${estimate.windowKey.endsWith(":primary") ? "5h" : t("common.quota.weekly")}`;
    return windowKey === "monthly" ? t("common.quota.monthly") : sourceWindowLabel({ windowKey, model: estimate.model }, t);
  };

  return (
    <section aria-label={t("quotaEstimates.title")} title={dense ? t("quotaEstimates.disclaimer") : undefined} className={dense ? "space-y-1 text-[10px] font-normal text-muted-foreground" : compact ? "mt-3 space-y-1.5 text-[11px] text-muted-foreground" : "space-y-2 rounded-md border bg-background/60 px-3 py-2 text-xs text-muted-foreground"}>
      <h4 className="text-[11px] font-semibold uppercase tracking-wider">{t(dense ? "quotaEstimates.shortTitle" : "quotaEstimates.title")}</h4>
      {state === "error" ? <p role="status">{t(dense ? "quotaEstimates.unknown" : "quotaEstimates.failed")}</p> : state === "loading" ? <p role="status">{t(dense ? "quotaEstimates.unknown" : "quotaEstimates.loading")}</p> : !windows?.length ? <p>{t(dense ? "quotaEstimates.insufficient" : "quotaEstimates.noEvidence")}</p> : windows.map((estimate) => (
        <div key={JSON.stringify([estimate.windowKey, estimate.model])} className="min-w-0 space-y-0.5">
          <p className="break-words" title={dense ? t(`quotaEstimates.reasons.${estimate.reason}`) : undefined}><span className="font-medium text-foreground">{label(estimate)}</span>{" · "}
            {estimate.status === "estimated" && estimate.estimatedTotalTokens !== null && estimate.remainingTokens !== null
              ? t(estimate.estimateKind === "recorded_lower_bound" ? dense ? "quotaEstimates.lowerDenseValues" : "quotaEstimates.lowerValues" : dense ? "quotaEstimates.denseValues" : "quotaEstimates.values", { total: formatCompactNumber(estimate.estimatedTotalTokens), remaining: formatCompactNumber(estimate.remainingTokens) })
              : <span aria-label={t(`quotaEstimates.reasons.${estimate.reason}`)}>{t(dense ? estimate.status === "insufficient" ? "quotaEstimates.insufficient" : "quotaEstimates.unknown" : `quotaEstimates.reasons.${estimate.reason}`)}</span>}
          </p>
          {!compact && estimate.status === "estimated" ? <p className="tabular-nums">{t("quotaEstimates.evidence", { tokens: formatCompactNumber(estimate.observedTokens), delta: (estimate.usedRatioDelta * 100).toFixed(1), samples: estimate.sampleCount, requests: estimate.requestCount })}</p> : null}
          {!compact && estimate.boundaryUncertaintyTokens > 0 ? <p>{t("quotaEstimates.boundary", { tokens: formatCompactNumber(estimate.boundaryUncertaintyTokens) })}</p> : null}
          {!compact && estimate.estimateKind === "recorded_lower_bound" ? <p>{t("quotaEstimates.lowerNotice")}</p> : null}
          {!compact && estimate.evidenceStart && estimate.evidenceEnd ? <p>{formatDateTimeInline(estimate.evidenceStart)} – {formatDateTimeInline(estimate.evidenceEnd)}</p> : null}
          {!compact && estimate.totalTokensLow !== null && estimate.totalTokensHigh !== null && formatCompactNumber(estimate.totalTokensLow) !== formatCompactNumber(estimate.totalTokensHigh) ? <p>{t("quotaEstimates.range", { low: formatCompactNumber(estimate.totalTokensLow), high: formatCompactNumber(estimate.totalTokensHigh) })}</p> : null}
        </div>
      ))}
      {!dense ? <p className="text-[10px]">{t("quotaEstimates.disclaimer")}</p> : null}
      {(account.quotaSharedCount ?? 0) > 1 ? <p className="text-[10px]">{t("quotaEstimates.shared", { count: account.quotaSharedCount })}</p> : null}
      {!compact ? <details className="pt-1">
        <summary className="cursor-pointer">{t("quotaEstimates.method")}</summary>
        <p className="mt-1">{t("quotaEstimates.formula")}</p>
        <p className="mt-1">{t("quotaEstimates.caveats")}</p>
      </details> : null}
    </section>
  );
}
