import type { DistributionMetric } from "./distribution-metric-toggle";
import { formatCompactNumber } from "@/utils/formatters";

export function formatDistributionMetricValue(
  value: number,
  metric: DistributionMetric,
): string {
  if (metric === "cost" && value > 0 && value < 0.01) return "<$0.01";
  const formatted = formatCompactNumber(value);
  return metric === "cost" ? `$${formatted}` : formatted;
}
