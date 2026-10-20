export type AccountTrendPoint = {
  t: string;
  v: number | null;
};

export type AccountTrendSeries = {
  key: string;
  label: string;
  points: AccountTrendPoint[];
};

export type MergedPoint = {
  t: string;
  time: number | string;
  primary: number | null;
  secondary: number | null;
  secondaryScheduled: number | null;
  additional: (number | null)[];
};

function timestamp(t: string): number | string {
  const parsed = Date.parse(t);
  return Number.isFinite(parsed) ? parsed : t;
}

export function mergeAccountTrendPoints(
  primary: AccountTrendPoint[],
  secondary: AccountTrendPoint[],
  secondaryScheduled: AccountTrendPoint[] = [],
  additional: AccountTrendSeries[] = [],
): MergedPoint[] {
  const timestamps = new Map<number | string, string>();
  const maps = [primary, secondary, secondaryScheduled, ...additional.map((series) => series.points)]
    .map((points) => new Map(points.map((point) => {
      const time = timestamp(point.t);
      timestamps.set(time, point.t);
      return [time, point.v] as const;
    })));
  const additionalMaps = maps.slice(3);

  return Array.from(timestamps, ([time, t]) => ({
    t,
    time,
    primary: maps[0].get(time) ?? null,
    secondary: maps[1].get(time) ?? null,
    secondaryScheduled: maps[2].get(time) ?? null,
    additional: additionalMaps.map((points) => points.get(time) ?? null),
  })).sort((a, b) => typeof a.time === "number" && typeof b.time === "number"
    ? a.time - b.time
    : a.t.localeCompare(b.t));
}
