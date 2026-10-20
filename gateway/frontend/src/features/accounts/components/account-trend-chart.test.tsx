import { describe, expect, it } from "vitest";

import { mergeAccountTrendPoints } from "@/features/accounts/account-trend-data";

const FIRST = "2026-10-11T00:00:00.000Z";
const SECOND = "2026-10-11T01:00:00.000Z";
const THIRD = "2026-10-11T02:00:00.000Z";

describe("mergeAccountTrendPoints", () => {
  it("sorts the union of asynchronous primary, weekly, and planned observations by actual time", () => {
    const merged = mergeAccountTrendPoints(
      [{ t: THIRD, v: 75 }, { t: FIRST, v: 80 }],
      [{ t: SECOND, v: 40 }],
      [{ t: THIRD, v: 35 }, { t: SECOND, v: 36 }],
    );

    expect(merged).toEqual([
      { t: FIRST, time: Date.parse(FIRST), primary: 80, secondary: null, secondaryScheduled: null, additional: [] },
      { t: SECOND, time: Date.parse(SECOND), primary: null, secondary: 40, secondaryScheduled: 36, additional: [] },
      { t: THIRD, time: Date.parse(THIRD), primary: 75, secondary: null, secondaryScheduled: 35, additional: [] },
    ]);
  });

  it("preserves explicit unknown samples and missing observations as gaps while keeping observed zero", () => {
    const merged = mergeAccountTrendPoints(
      [{ t: FIRST, v: 90 }, { t: SECOND, v: null }, { t: THIRD, v: 0 }],
      [{ t: FIRST, v: 0 }, { t: THIRD, v: null }],
    );

    expect(merged.map((point) => point.primary)).toEqual([90, null, 0]);
    expect(merged.map((point) => point.secondary)).toEqual([0, null, null]);
    expect(merged.map((point) => point.secondaryScheduled)).toEqual([null, null, null]);
    expect(merged).toHaveLength(3);
  });

  it("keeps model-specific history independent of generic quota and other models", () => {
    const merged = mergeAccountTrendPoints(
      [{ t: FIRST, v: 80 }],
      [],
      [],
      [
        { key: "primary", label: "Sonnet", points: [{ t: SECOND, v: 20 }, { t: THIRD, v: null }] },
        { key: "seven_day.opus", label: "Opus", points: [{ t: FIRST, v: 0 }, { t: THIRD, v: 45 }] },
      ],
    );

    expect(merged.map((point) => point.t)).toEqual([FIRST, SECOND, THIRD]);
    expect(merged.map((point) => point.primary)).toEqual([80, null, null]);
    expect(merged.map((point) => point.secondary)).toEqual([null, null, null]);
    expect(merged.map((point) => point.additional)).toEqual([[null, 0], [20, null], [null, 45]]);
  });

  it("retains model-only singleton observations without manufacturing endpoints or a weekly plan", () => {
    expect(mergeAccountTrendPoints([], [], [], [
      { key: "seven_day_sonnet", label: "Sonnet", points: [{ t: SECOND, v: 63 }] },
    ])).toEqual([
      { t: SECOND, time: Date.parse(SECOND), primary: null, secondary: null, secondaryScheduled: null, additional: [63] },
    ]);
    expect(mergeAccountTrendPoints([], [])).toEqual([]);
  });

  it("aligns equivalent timestamps even when the provider uses another timezone offset", () => {
    const offsetTimestamp = "2026-10-11T09:00:00+09:00";
    const merged = mergeAccountTrendPoints(
      [{ t: FIRST, v: 88 }],
      [{ t: offsetTimestamp, v: 44 }],
    );

    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({
      time: Date.parse(FIRST),
      primary: 88,
      secondary: 44,
    });
  });
});
