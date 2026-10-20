import { useId, useMemo } from "react";
import { useTranslation } from "react-i18next";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "@/components/lazy-recharts";

import { useChartColors } from "@/hooks/use-chart-colors";
import { useReducedMotion } from "@/hooks/use-reduced-motion";
import { formatChartDateTime } from "@/utils/formatters";
import { mergeAccountTrendPoints, type AccountTrendPoint, type AccountTrendSeries, type MergedPoint } from "@/features/accounts/account-trend-data";


function formatXTick(value: number | string): string {
  return (typeof value === "number" ? new Date(value).toISOString() : value).slice(5, 10);
}

type ChartTooltipPayloadEntry = {
  dataKey?: string | number;
  value?: number | null;
  color?: string;
};

type ChartTooltipProps = {
  active?: boolean;
  payload?: ChartTooltipPayloadEntry[];
  label?: string | number;
  labels: Record<string, string>;
};

function CustomTooltip({ active, payload, label, labels }: ChartTooltipProps) {
  const observations = payload?.filter((entry) => entry.value != null);
  if (!active || !observations?.length) return null;
  const heading = formatChartDateTime(typeof label === "number" ? new Date(label).toISOString() : label);
  return (
    <div className="rounded-lg border bg-popover px-3 py-2 text-popover-foreground shadow-md">
      <p className="mb-1 text-[11px] text-muted-foreground">{heading}</p>
      {observations.map((entry) => (
        <div key={entry.dataKey} className="flex items-center gap-2 text-xs">
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{ backgroundColor: entry.color }}
          />
          <span className="text-muted-foreground">{labels[String(entry.dataKey)]}</span>
          <span className="ml-auto tabular-nums font-medium">{entry.value?.toFixed(1)}%</span>
        </div>
      ))}
    </div>
  );
}

const CHART_MARGIN = { top: 4, right: 8, bottom: 0, left: 0 } as const;
const DAY_MS = 86_400_000;
const EMPTY_TREND_POINTS: AccountTrendPoint[] = [];
const EMPTY_ADDITIONAL_SERIES: AccountTrendSeries[] = [];

export type AccountTrendChartProps = {
  primary: AccountTrendPoint[];
  secondary: AccountTrendPoint[];
  secondaryScheduled?: AccountTrendPoint[];
  additional?: AccountTrendSeries[];
  windowStart?: string;
  windowEnd?: string;
};

export function AccountTrendChart({
  primary,
  secondary,
  secondaryScheduled = EMPTY_TREND_POINTS,
  additional = EMPTY_ADDITIONAL_SERIES,
  windowStart,
  windowEnd,
}: AccountTrendChartProps) {
  const { t } = useTranslation();
  const chartColors = useChartColors();
  const reducedMotion = useReducedMotion();
  const gradientPrefix = `account-trend-${useId().replace(/:/g, "")}`;
  const data = useMemo(
    () => mergeAccountTrendPoints(primary, secondary, secondaryScheduled, additional),
    [primary, secondary, secondaryScheduled, additional],
  );
  const series = [
    {
      key: "primary",
      dataKey: "primary",
      label: t("accounts.trend.series.primary", { defaultValue: "Primary" }),
      points: primary,
      color: chartColors[0],
      value: (point: MergedPoint) => point.primary,
      scheduled: false,
    },
    {
      key: "secondary",
      dataKey: "secondary",
      label: t("accounts.trend.series.secondary", { defaultValue: "Secondary" }),
      points: secondary,
      color: chartColors[1],
      value: (point: MergedPoint) => point.secondary,
      scheduled: false,
    },
    {
      key: "secondaryScheduled",
      dataKey: "secondaryScheduled",
      label: t("accounts.trend.series.secondaryScheduled", { defaultValue: "Weekly plan" }),
      points: secondaryScheduled,
      color: chartColors[1],
      value: (point: MergedPoint) => point.secondaryScheduled,
      scheduled: true,
    },
    ...additional.map((item, index) => ({
      key: `additional-${item.key}`,
      dataKey: `additional[${index}]`,
      label: item.label,
      points: item.points,
      color: chartColors[(index + 2) % chartColors.length],
      value: (point: MergedPoint) => point.additional[index],
      scheduled: false,
    })),
  ];
  const labels = Object.fromEntries(series.map((item) => [item.dataKey, item.label]));
  const numericTime = data.length > 0 && data.every((point) => typeof point.time === "number");
  const start = windowStart ? Date.parse(windowStart) : NaN;
  const end = windowEnd ? Date.parse(windowEnd) : NaN;
  const domainStart = numericTime
    ? Math.min(data[0].time as number, Number.isFinite(start) ? start : Infinity)
    : 0;
  const domainEnd = numericTime
    ? Math.max(data[data.length - 1].time as number, Number.isFinite(end) ? end : -Infinity)
    : 0;
  const ticks: number[] = [];
  if (numericTime) {
    const step = Math.max(1, Math.ceil((domainEnd - domainStart) / DAY_MS / 7)) * DAY_MS;
    for (let time = Math.ceil(domainStart / DAY_MS) * DAY_MS; time <= domainEnd; time += step) {
      ticks.push(time);
    }
    if (ticks.length === 0) ticks.push(domainStart);
  }

  if (data.length === 0) {
    return (
      <div className="flex h-[200px] items-center justify-center text-xs text-muted-foreground">
        {t("accounts.trend.empty")}
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={data} margin={CHART_MARGIN}>
        <defs>
          {series.filter((item) => !item.scheduled).map((item) => (
            <linearGradient key={item.key} id={`${gradientPrefix}-${item.dataKey}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={item.color} stopOpacity={0.12} />
              <stop offset="100%" stopColor={item.color} stopOpacity={0} />
            </linearGradient>
          ))}
        </defs>
        <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="currentColor" opacity={0.06} />
        <XAxis
          dataKey={numericTime ? "time" : "t"}
          type={numericTime ? "number" : "category"}
          scale={numericTime ? "time" : "auto"}
          domain={numericTime ? [domainStart, domainEnd] : undefined}
          ticks={numericTime ? ticks : undefined}
          tickFormatter={formatXTick}
          tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
          tickLine={false}
          axisLine={false}
          minTickGap={50}
          dy={4}
        />
        <YAxis
          domain={[0, 100]}
          ticks={[0, 25, 50, 75, 100]}
          tickFormatter={(v: number) => `${v}%`}
          tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
          tickLine={false}
          axisLine={false}
          width={38}
        />
        <Tooltip
          content={<CustomTooltip labels={labels} />}
          cursor={{ stroke: "hsl(var(--border))", strokeWidth: 1 }}
        />
        {series.filter((item) => item.points.length > 0).map((item, seriesIndex) => {
          const dot = ({ cx, cy, index }: { cx?: number; cy?: number; index?: number }) => {
            if (index == null || cx == null || cy == null || item.value(data[index]) == null) return <g />;
            const before = index > 0 ? item.value(data[index - 1]) : null;
            const after = index + 1 < data.length ? item.value(data[index + 1]) : null;
            return before == null && after == null
              ? <circle cx={cx} cy={cy} r={2.5} fill={item.color} />
              : <g />;
          };
          const commonProps = {
            dataKey: item.dataKey,
            stroke: item.color,
            dot,
            activeDot: { r: 3, strokeWidth: 1.5, fill: "hsl(var(--popover))" },
            connectNulls: false,
            isAnimationActive: !reducedMotion,
            animationDuration: 500,
            animationBegin: item.scheduled ? 150 : seriesIndex === 0 ? 0 : 100,
          };
          return item.scheduled ? (
            <Line key={item.key} {...commonProps} type="linear" strokeWidth={1.25} strokeDasharray="5 5" />
          ) : (
            <Area
              key={item.key}
              {...commonProps}
              type="monotone"
              strokeWidth={1.5}
              fill={`url(#${gradientPrefix}-${item.dataKey})`}
            />
          );
        })}
      </AreaChart>
    </ResponsiveContainer>
  );
}
