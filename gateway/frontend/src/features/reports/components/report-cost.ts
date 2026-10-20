import type { TFunction } from "i18next";
import { formatCurrency } from "@/utils/formatters";

// Missing cost is unknown; a positive sub-cent subtotal must not look free.
export function formatReportCost(costUsd: number, unknownCostCount = 0, t: TFunction): string {
  const knownCost = costUsd > 0 && costUsd < 0.01 ? "<$0.01" : formatCurrency(costUsd);
  if (unknownCostCount <= 0) return knownCost;
  if (costUsd === 0) return t("reportMonitoring.pricing.unknown");
  return t("reportMonitoring.pricing.knownSubtotal", { cost: knownCost });
}
