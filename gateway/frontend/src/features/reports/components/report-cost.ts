import type { TFunction } from "i18next";
import { formatCurrency } from "@/utils/formatters";

// The backend reports a known subtotal; missing prices must never look free.
export function formatReportCost(costUsd: number, unknownCostCount = 0, t: TFunction): string {
  if (unknownCostCount <= 0) return formatCurrency(costUsd);
  if (costUsd === 0) return t("reportMonitoring.pricing.unknown");
  return t("reportMonitoring.pricing.knownSubtotal", { cost: formatCurrency(costUsd) });
}
