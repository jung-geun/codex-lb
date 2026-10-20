import { lazy, Suspense, useMemo } from "react";
import { useTranslation } from "react-i18next";
import type { AccountCostEntry, TypeCostEntry } from "../schemas";
import { formatReportCost } from "./report-cost";

export type ReportSourceBreakdownProps = {
  byAccount: AccountCostEntry[];
  byType: TypeCostEntry[];
};

const ProviderDistribution = lazy(() => import("./model-distribution-donut").then((module) => ({
  default: module.ModelDistributionDonut,
})));

export function ReportSourceBreakdown({ byAccount, byType }: ReportSourceBreakdownProps) {
  const { t } = useTranslation();
  const providerDistribution = useMemo(() => {
    const totalCost = byType.reduce((sum, entry) => sum + entry.costUsd, 0);
    return byType.map((entry) => ({
      model: entry.accountType,
      costUsd: entry.costUsd,
      requests: entry.requests,
      unknownCostCount: entry.unknownCostCount,
      percentage: totalCost > 0 ? (entry.costUsd / totalCost) * 100 : 0,
    }));
  }, [byType]);
  const rows = byAccount.map((entry) => ({
    ...entry,
    id: JSON.stringify([entry.accountType, entry.accountId, entry.modelSourceId]),
    name: entry.alias || entry.accountId || t("reportMonitoring.breakdown.unattributed"),
    detail: entry.accountId,
  }));

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <section className="min-w-0 rounded-xl border bg-card p-5" aria-labelledby="report-breakdown-accounts">
          <h2 id="report-breakdown-accounts" className="mb-3 text-sm font-semibold">{t("reportMonitoring.breakdown.byAccount")}</h2>
          <div className="max-h-64 overflow-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-card text-muted-foreground">
                <tr className="border-b text-left">
                  <th scope="col" className="pb-2 pr-3 font-medium">{t("reportMonitoring.filters.accounts")}</th>
                  <th scope="col" className="pb-2 pr-3 font-medium">{t("reportMonitoring.filters.type")}</th>
                  <th scope="col" className="pb-2 pr-3 text-right font-medium">{t("reports.dailyBreakdown.columns.reqs")}</th>
                  <th scope="col" className="pb-2 text-right font-medium">{t("reportMonitoring.pricing.apiEquivalent")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} className="border-b border-border/50 last:border-0">
                    <td className="py-2 pr-3">
                      <div className="break-all font-medium">{row.name}</div>
                      {row.detail && row.detail !== row.name ? <div className="break-all text-muted-foreground">{row.detail}</div> : null}
                    </td>
                    <td className="py-2 pr-3">{row.accountType}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{row.requests}</td>
                    <td className="py-2 text-right tabular-nums">
                      {formatReportCost(row.costUsd, row.unknownCostCount, t)}
                      {(row.unknownCostCount ?? 0) > 0 ? <div className="text-muted-foreground">{t("reportMonitoring.pricing.unknownCount", { count: row.unknownCostCount })}</div> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.length === 0 ? <p className="py-4 text-xs text-muted-foreground">{t("reportMonitoring.breakdown.empty")}</p> : null}
          </div>
        </section>
      <Suspense fallback={<div className="h-[220px] rounded-xl border bg-card" />}>
        <ProviderDistribution data={providerDistribution} dimension="provider" />
      </Suspense>
    </div>
  );
}
