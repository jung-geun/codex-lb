import { useTranslation } from "react-i18next";
import type { AccountCostEntry, TypeCostEntry } from "../schemas";
import { formatReportCost } from "./report-cost";

export type ReportSourceBreakdownProps = {
  byAccount: AccountCostEntry[];
  byType: TypeCostEntry[];
};

export function ReportSourceBreakdown({ byAccount, byType }: ReportSourceBreakdownProps) {
  const { t } = useTranslation();
  const tables = [
    {
      id: "accounts",
      title: t("reportMonitoring.breakdown.byAccount"),
      label: t("reportMonitoring.filters.accounts"),
      rows: byAccount.map((entry) => ({
        ...entry,
        id: JSON.stringify([entry.accountType, entry.accountId, entry.modelSourceId]),
        name: entry.alias || entry.accountId || t("reportMonitoring.breakdown.unattributed"),
        detail: entry.accountId,
      })),
    },
    {
      id: "types",
      title: t("reportMonitoring.breakdown.byType"),
      label: t("reportMonitoring.filters.type"),
      rows: byType.map((entry) => ({
        ...entry,
        id: entry.accountType,
        name: entry.accountType,
        detail: null,
      })),
    },
  ];

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      {tables.map((table) => (
        <section key={table.id} className="min-w-0 rounded-xl border bg-card p-5" aria-labelledby={`report-breakdown-${table.id}`}>
          <h2 id={`report-breakdown-${table.id}`} className="mb-3 text-sm font-semibold">{table.title}</h2>
          <div className="max-h-64 overflow-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-card text-muted-foreground">
                <tr className="border-b text-left">
                  <th scope="col" className="pb-2 pr-3 font-medium">{table.label}</th>
                  {table.id === "accounts" ? <th scope="col" className="pb-2 pr-3 font-medium">{t("reportMonitoring.filters.type")}</th> : null}
                  <th scope="col" className="pb-2 pr-3 text-right font-medium">{t("reports.dailyBreakdown.columns.reqs")}</th>
                  <th scope="col" className="pb-2 text-right font-medium">{t("reportMonitoring.pricing.apiEquivalent")}</th>
                </tr>
              </thead>
              <tbody>
                {table.rows.map((row) => (
                  <tr key={row.id} className="border-b border-border/50 last:border-0">
                    <td className="py-2 pr-3">
                      <div className="break-all font-medium">{row.name}</div>
                      {row.detail && row.detail !== row.name ? <div className="break-all text-muted-foreground">{row.detail}</div> : null}
                    </td>
                    {table.id === "accounts" ? <td className="py-2 pr-3">{row.accountType}</td> : null}
                    <td className="py-2 pr-3 text-right tabular-nums">{row.requests}</td>
                    <td className="py-2 text-right tabular-nums">
                      {formatReportCost(row.costUsd, row.unknownCostCount, t)}
                      {(row.unknownCostCount ?? 0) > 0 ? <div className="text-muted-foreground">{t("reportMonitoring.pricing.unknownCount", { count: row.unknownCostCount })}</div> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {table.rows.length === 0 ? <p className="py-4 text-xs text-muted-foreground">{t("reportMonitoring.breakdown.empty")}</p> : null}
          </div>
        </section>
      ))}
    </div>
  );
}
