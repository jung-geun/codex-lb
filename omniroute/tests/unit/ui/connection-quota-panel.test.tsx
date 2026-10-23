// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.unmock("next-intl");
import { NextIntlClientProvider } from "next-intl";
import en from "@/i18n/messages/en.json";
import ko from "@/i18n/messages/ko.json";

import ConnectionQuotaPanel from "@/app/(dashboard)/dashboard/providers/[id]/components/ConnectionQuotaPanel";
import type { ProviderQuotaCacheEntry } from "@/app/(dashboard)/dashboard/providers/[id]/hooks/useProviderQuota";
import ConnectionRow, {
  type ConnectionRowConnection,
} from "@/app/(dashboard)/dashboard/providers/[id]/components/ConnectionRow";

const cleanupCallbacks: Array<() => void> = [];

beforeEach(() => {
  (
    globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ windows: [] }),
    })
  );
});

afterEach(() => {
  while (cleanupCallbacks.length) cleanupCallbacks.pop()?.();
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

function mount(element: React.ReactElement, locale: "en" | "ko" = "en"): HTMLElement {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  cleanupCallbacks.push(() => act(() => root.unmount()));
  act(() =>
    root.render(
      <NextIntlClientProvider locale={locale} messages={locale === "en" ? en : ko} timeZone="UTC">
        {element}
      </NextIntlClientProvider>
    )
  );
  return container;
}

const futureReset = (hours: number) => new Date(Date.now() + hours * 3_600_000).toISOString();

function panelProps(overrides: Partial<Parameters<typeof ConnectionQuotaPanel>[0]> = {}) {
  return {
    providerId: "codex",
    connection: { id: "c1", provider: "codex", name: "Codex account" } as ConnectionRowConnection,
    cache: {
      quotas: {
        session: { used: 0, total: 0, remainingPercentage: 22, resetAt: futureReset(2) },
        weekly: { used: 0, total: 0, remainingPercentage: 43, resetAt: futureReset(96) },
      },
      plan: "pro",
      message: null,
      fetchedAt: new Date().toISOString(),
    } as ProviderQuotaCacheEntry,
    refreshing: false,
    onRefresh: vi.fn(),
    ...overrides,
  };
}

describe("ConnectionQuotaPanel", () => {
  it("shows Codex subscription graphs by default with distinct used and remaining percentages", () => {
    const el = mount(<ConnectionQuotaPanel {...panelProps()} />);
    const bars = Array.from(el.querySelectorAll('[role="progressbar"]'));
    expect(bars).toHaveLength(2);
    expect(bars.map((bar) => bar.getAttribute("aria-valuenow"))).toEqual(["22", "43"]);
    expect(bars.every((bar) => bar.getAttribute("aria-valuemin") === "0")).toBe(true);
    expect(bars.every((bar) => bar.getAttribute("aria-valuemax") === "100")).toBe(true);
    expect(bars[0].getAttribute("aria-valuetext")).toBe("Used 78%; Remaining 22%");
    expect(bars[1].getAttribute("aria-valuetext")).toBe("Used 57%; Remaining 43%");
    expect(el.textContent).not.toMatch(/78\s*\/\s*100|57\s*\/\s*100/);
  });

  it("shows reset countdowns and separate absolute reset times without expanding", () => {
    const sessionReset = futureReset(2);
    const weeklyReset = futureReset(96);
    const props = panelProps({
      cache: {
        quotas: {
          session: { used: 78, total: 100, remainingPercentage: 22, resetAt: sessionReset },
          weekly: { used: 57, total: 100, remainingPercentage: 43, resetAt: weeklyReset },
        },
        fetchedAt: new Date().toISOString(),
      },
    });
    const el = mount(<ConnectionQuotaPanel {...props} />);
    expect(el.textContent).toMatch(/resets in/i);
    const times = Array.from(el.querySelectorAll("time[datetime]"));
    expect(times.map((time) => time.getAttribute("datetime"))).toEqual([sessionReset, weeklyReset]);
    for (const time of times) {
      expect(time.textContent).toMatch(/\d/);
      expect(time.textContent).toMatch(/UTC|GMT|[A-Z]{2,5}|[+-]\d{2}/);
      expect(time.textContent).not.toMatch(/resets in/i);
    }
  });

  it("shows all Claude percentage-only subscription graphs by default", () => {
    const el = mount(
      <ConnectionQuotaPanel
        {...panelProps({
          providerId: "claude",
          connection: {
            id: "c2",
            provider: "claude",
            name: "Claude account",
          } as ConnectionRowConnection,
          cache: {
            quotas: {
              "session (5h)": {
                used: 0,
                total: 0,
                remainingPercentage: 30,
                resetAt: futureReset(3),
              },
              "weekly (7d)": {
                used: 0,
                total: 0,
                remainingPercentage: 80,
                resetAt: futureReset(120),
              },
            },
            plan: "Claude Max",
            message: null,
            fetchedAt: new Date().toISOString(),
          },
        })}
      />
    );
    expect(
      Array.from(el.querySelectorAll('[role="progressbar"]')).map((bar) =>
        bar.getAttribute("aria-valuenow")
      )
    ).toEqual(["30", "80"]);
    expect(el.textContent).toMatch(/70%.*used|used.*70%/i);
    expect(el.textContent).toMatch(/30%.*remaining|remaining.*30%/i);
  });

  it("shows every Antigravity model subscription graph and keeps credits separate", () => {
    const modelQuota = (used: number, remaining: number) => ({
      used,
      total: 100,
      remainingPercentage: remaining,
      resetAt: futureReset(24),
    });
    const el = mount(
      <ConnectionQuotaPanel
        {...panelProps({
          providerId: "antigravity",
          connection: {
            id: "c3",
            provider: "antigravity",
            name: "Antigravity account",
          } as ConnectionRowConnection,
          cache: {
            quotas: {
              "gemini-2.5-pro": modelQuota(10, 90),
              "claude-4-sonnet": modelQuota(4, 96),
              "gemini-3-flash": modelQuota(70, 30),
              "gpt-oss-120b": modelQuota(2, 98),
              "claude-opus-4-6": modelQuota(1, 99),
              credits: { remaining: 120 },
            },
            plan: "Ultra",
            message: null,
            fetchedAt: new Date().toISOString(),
          },
        })}
      />
    );
    const bars = Array.from(el.querySelectorAll('[role="progressbar"]'));
    expect(bars).toHaveLength(5);
    expect(
      bars.map((bar) => Number(bar.getAttribute("aria-valuenow"))).sort((a, b) => a - b)
    ).toEqual([30, 90, 96, 98, 99]);
    for (const model of [
      "gemini-2.5-pro",
      "claude-4-sonnet",
      "gemini-3-flash",
      "gpt-oss-120b",
      "claude-opus-4-6",
    ]) {
      expect(el.textContent).toContain(model);
    }
    expect(el.textContent).not.toContain("+2");
    expect(el.textContent).toContain("120 credits");
    expect(bars.some((bar) => /credits/i.test(bar.getAttribute("aria-label") ?? ""))).toBe(false);
  });

  it("shows all AGY model graphs alongside rolling subscription windows", () => {
    const el = mount(
      <ConnectionQuotaPanel
        {...panelProps({
          providerId: "agy",
          connection: {
            id: "c4",
            provider: "agy",
            name: "AGY account",
          } as ConnectionRowConnection,
          cache: {
            quotas: {
              gemini_weekly: {
                used: 0,
                total: 0,
                remainingPercentage: 55,
                resetAt: futureReset(90),
              },
              "gemini-2.5-pro": {
                used: 10,
                total: 100,
                remainingPercentage: 90,
                resetAt: futureReset(24),
              },
              "claude-4-sonnet": {
                used: 4,
                total: 100,
                remainingPercentage: 96,
                resetAt: futureReset(24),
              },
            },
            plan: "Pro",
            message: null,
            fetchedAt: new Date().toISOString(),
          },
        })}
      />
    );
    expect(el.querySelectorAll('[role="progressbar"]')).toHaveLength(3);
    expect(el.textContent).toContain("gemini-2.5-pro");
    expect(el.textContent).toContain("claude-4-sonnet");
    expect(el.textContent).not.toContain("+1");
  });

  it("surfaces an upstream error message when only a message row exists", () => {
    const el = mount(
      <ConnectionQuotaPanel
        {...panelProps({
          cache: {
            quotas: {},
            plan: null,
            message: "usage endpoint rate limited",
            fetchedAt: new Date().toISOString(),
          },
        })}
      />
    );
    expect(el.textContent).toContain("usage endpoint rate limited");
  });

  it("surfaces a top-level message alongside parsed quota rows (xiaomi-mimo fallback)", () => {
    const el = mount(
      <ConnectionQuotaPanel
        {...panelProps({
          providerId: "xiaomi-mimo",
          connection: {
            id: "c5",
            provider: "xiaomi-mimo",
            name: "MiMo account",
          } as ConnectionRowConnection,
          cache: {
            quotas: {
              monthly: {
                used: 14_954_038_035,
                total: 38_000_000_000,
                remainingPercentage: 60.6,
                resetAt: futureReset(72),
              },
            },
            plan: "Xiaomi MiMo Token Plan (OmniRoute-tracked)",
            message: "MiMo console cookie missing or expired — set the cookie to read live quota.",
            fetchedAt: new Date().toISOString(),
          },
        })}
      />
    );
    // the row count must not gate the hint: without this the fallback counters
    // render as if they were live console numbers.
    expect(el.textContent).toContain("MiMo console cookie missing or expired");
  });

  it("offers a first fetch when no cache exists yet and fires onRefresh", () => {
    const onRefresh = vi.fn();
    const el = mount(<ConnectionQuotaPanel {...panelProps({ cache: null, onRefresh })} />);
    expect(el.textContent).toContain("No usage data yet");
    const refresh = el.querySelector<HTMLButtonElement>("button[title='Refresh usage']")!;
    act(() => refresh.click());
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});

function subscriptionSection(container: HTMLElement, locale: "en" | "ko" = "en") {
  const text = locale === "en" ? en.providers : ko.providers;
  const heading = Array.from(container.querySelectorAll("h4")).find(
    (element) => element.textContent === text.subscriptionQuotaTitle
  );
  const section = heading?.closest("section") ?? null;
  expect(section?.getAttribute("aria-labelledby")).toBe(heading?.id);
  return section;
}

describe("default native subscription allowance details", () => {
  for (const locale of ["en", "ko"] as const) {
    for (const provider of ["codex", "claude", "antigravity", "agy"]) {
      it(`keeps ${provider} allowance visible and empirical capacity opt-in (${locale})`, async () => {
        const text = locale === "en" ? en.providers : ko.providers;
        const props = panelProps({
          providerId: provider,
          connection: { id: "account/with space", provider },
          cache: {
            quotas: {
              "session (5h)": {
                used: 23,
                total: 100,
                remainingPercentage: 77,
                fractionReported: true,
                resetAt: futureReset(3),
              },
            },
            fetchedAt: new Date().toISOString(),
          },
        });
        const el = mount(<ConnectionQuotaPanel {...props} />, locale);
        const section = subscriptionSection(el, locale);
        expect(section).not.toBeNull();
        const bar = section!.querySelector('[role="progressbar"]');
        expect(bar?.getAttribute("aria-valuenow")).toBe("77");
        expect(bar?.getAttribute("aria-label")).toBeTruthy();
        expect(section!.textContent).toContain("23%");
        expect(section!.textContent).toContain("77%");
        expect(section!.textContent!.toLowerCase()).toContain(locale === "ko" ? "사용" : "used");
        expect(section!.textContent!.toLowerCase()).toContain(
          locale === "ko" ? "잔여" : "remaining"
        );
        const reset = section!.querySelector("time[datetime]");
        expect(reset).not.toBeNull();
        const resetTime = Date.parse(reset!.getAttribute("datetime")!);
        expect(reset!.textContent).toBe(
          new Intl.DateTimeFormat(locale, {
            year: "numeric",
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
            timeZoneName: "short",
          }).format(resetTime)
        );
        expect(section!.textContent).toContain(locale === "ko" ? "후 초기화" : "resets in");
        expect(fetch).not.toHaveBeenCalled();
        const toggle = el.querySelector<HTMLButtonElement>(
          `button[aria-label="${text.quotaCapacityDetails}"]`
        );
        expect(toggle?.getAttribute("aria-expanded")).toBe("false");
        await act(async () => toggle!.click());
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(fetch).toHaveBeenCalledWith(
          "/api/providers/account%2Fwith%20space/quota-capacity",
          expect.objectContaining({ cache: "no-store", signal: expect.any(AbortSignal) })
        );
        expect(toggle?.getAttribute("aria-expanded")).toBe("true");
      });
    }
  }

  it("renders actual Antigravity normalized fraction data as percentages, not token counts", () => {
    const el = mount(
      <ConnectionQuotaPanel
        {...panelProps({
          providerId: "antigravity",
          connection: { id: "normalized", provider: "antigravity" },
          cache: {
            quotas: {
              "gemini-3-flash": {
                used: 275,
                total: 1000,
                remainingPercentage: 72.5,
                fractionReported: true,
                unlimited: false,
                quotaSource: "retrieveUserQuota",
                resetAt: futureReset(4),
              },
            },
            fetchedAt: new Date().toISOString(),
          },
        })}
      />
    );
    const section = subscriptionSection(el);
    const bar = section!.querySelector('[role="progressbar"]');
    expect(bar?.getAttribute("aria-valuenow")).toBe("72.5");
    expect(bar?.getAttribute("aria-valuetext")).toBe("Used 27.5%; Remaining 72.5%");
    expect(section!.textContent).not.toMatch(/275|1,?000/);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("keeps scoped Claude and Spark window labels distinct and wrap-safe", () => {
    const quota = { used: 25, total: 100, remainingPercentage: 75, resetAt: futureReset(12) };
    const claude = mount(
      <ConnectionQuotaPanel
        {...panelProps({
          providerId: "claude",
          connection: { id: "scoped", provider: "claude" },
          cache: {
            quotas: { "session (5h)": quota, "weekly (7d)": quota },
            modelQuotas: { "weekly sonnet (7d)": quota, "weekly opus (7d)": quota },
            fetchedAt: new Date().toISOString(),
          },
        })}
      />
    );
    expect(claude.querySelectorAll('[role="progressbar"]')).toHaveLength(4);
    for (const label of ["weekly sonnet (7d)", "weekly opus (7d)"]) {
      expect(claude.querySelector(`[role="progressbar"][aria-label="${label}"]`)).not.toBeNull();
      const fullLabel = Array.from(claude.querySelectorAll("span")).find(
        (element) => element.textContent === label
      );
      expect(fullLabel?.className).toContain("break-words");
      expect(fullLabel?.className).not.toMatch(/truncate|whitespace-nowrap/);
    }
    const codex = mount(
      <ConnectionQuotaPanel
        {...panelProps({
          cache: {
            quotas: {
              session: quota,
              weekly: quota,
              gpt_5_3_codex_spark_session: quota,
              gpt_5_3_codex_spark_weekly: quota,
            },
            fetchedAt: new Date().toISOString(),
          },
        })}
      />
    );
    const bars = Array.from(codex.querySelectorAll('[role="progressbar"]'));
    expect(bars).toHaveLength(4);
    expect(bars[2].getAttribute("aria-label")).toMatch(/spark.*5h/i);
    expect(bars[3].getAttribute("aria-label")).toMatch(/spark.*7d/i);
  });

  for (const resetAt of [null, "invalid-reset"]) {
    it(`does not invent reset countdowns for ${String(resetAt)}`, () => {
      const el = mount(
        <ConnectionQuotaPanel
          {...panelProps({
            cache: {
              quotas: { session: { used: 25, total: 100, remainingPercentage: 75, resetAt } },
              fetchedAt: new Date().toISOString(),
            },
          })}
        />
      );
      const section = subscriptionSection(el);
      expect(section!.querySelector("time")).toBeNull();
      expect(section!.textContent).toContain(en.providers.subscriptionQuotaResetUnknown);
      expect(section!.textContent).not.toMatch(/resets in|Invalid Date/);
    });
  }

  it("keeps genuinely reported exhausted and untouched windows valid", () => {
    const el = mount(
      <ConnectionQuotaPanel
        {...panelProps({
          cache: {
            quotas: {
              session: { used: 100, total: 100, remainingPercentage: 0, resetAt: futureReset(2) },
              weekly: { used: 0, total: 100, remainingPercentage: 100, resetAt: futureReset(12) },
            },
            fetchedAt: new Date().toISOString(),
          },
        })}
      />
    );
    expect(
      Array.from(el.querySelectorAll('[role="progressbar"]')).map((bar) =>
        bar.getAttribute("aria-valuenow")
      )
    ).toEqual(["0", "100"]);
  });

  it("does not cap Antigravity at sixteen models or hide full labels behind +N", () => {
    const models = Array.from(
      { length: 21 },
      (_, index) => `gemini-test-long-provider-model-identifier-${index}-thinking-high`
    );
    const el = mount(
      <ConnectionQuotaPanel
        {...panelProps({
          providerId: "antigravity",
          connection: { id: "many", provider: "antigravity" },
          cache: {
            quotas: Object.fromEntries(
              models.map((name, index) => [
                name,
                {
                  used: index,
                  total: 100,
                  remainingPercentage: 100 - index,
                  fractionReported: true,
                  resetAt: futureReset(24),
                },
              ])
            ),
            fetchedAt: new Date().toISOString(),
          },
        })}
      />
    );
    const bars = Array.from(el.querySelectorAll('[role="progressbar"]'));
    expect(bars).toHaveLength(models.length);
    expect(bars.map((bar) => bar.getAttribute("aria-label"))).toEqual(models);
    for (const model of models) expect(el.textContent).toContain(model);
  });

  for (const provider of ["codex", "claude", "antigravity"]) {
    for (const scenario of ["unknown", "reset elapsed", "stale snapshot"] as const) {
      it(`never presents ${provider} ${scenario} data as valid progress or zero usage`, () => {
        const resetAt =
          scenario === "reset elapsed"
            ? new Date(Date.now() - 60_000).toISOString()
            : futureReset(2);
        const quota =
          scenario === "unknown"
            ? { resetAt, fractionReported: false }
            : { used: 83, total: 100, remainingPercentage: 17, resetAt, fractionReported: true };
        const el = mount(
          <ConnectionQuotaPanel
            {...panelProps({
              providerId: provider,
              connection: { id: "not-current", provider },
              cache: {
                quotas: { "session (5h)": quota },
                fetchedAt: new Date().toISOString(),
                ...(scenario === "stale snapshot" ? { _stale: true } : {}),
              },
            })}
          />
        );
        const section = subscriptionSection(el);
        expect(section).not.toBeNull();
        expect(section!.querySelector('[role="progressbar"][aria-valuenow]')).toBeNull();
        expect(section!.textContent).not.toMatch(/(?:^|\D)0%|100%/);
        const state =
          scenario === "unknown"
            ? en.providers.subscriptionQuotaUnknown
            : scenario === "reset elapsed"
              ? en.providers.subscriptionQuotaResetElapsed
              : en.providers.subscriptionQuotaStale;
        expect(section!.textContent).toContain(state);
        const bar = section!.querySelector('[role="progressbar"]');
        expect(bar?.getAttribute("aria-valuetext")).toBe(state);
        expect(bar?.children).toHaveLength(0);
        expect(section!.textContent).toContain("Used —");
        expect(section!.textContent).toContain("Remaining —");
        expect(fetch).not.toHaveBeenCalled();
      });
    }
  }

  it("does not turn Claude extra usage or Codex reset credits into subscription bars", () => {
    const claude = mount(
      <ConnectionQuotaPanel
        {...panelProps({
          providerId: "claude",
          connection: { id: "extra", provider: "claude" },
          cache: {
            quotas: {
              "session (5h)": {
                used: 12,
                total: 100,
                remainingPercentage: 88,
                resetAt: futureReset(2),
              },
            },
            extraUsage: {
              is_enabled: true,
              monthly_limit: 9000,
              used_credits: 1234,
              currency: "EUR",
              decimal_places: 2,
            },
            fetchedAt: new Date().toISOString(),
          },
        })}
      />
    );
    const allowance = subscriptionSection(claude);
    expect(allowance!.querySelectorAll('[role="progressbar"]')).toHaveLength(1);
    expect(allowance!.textContent).not.toContain("EUR");
    expect(claude.textContent).toContain("77.66 EUR");
    const codex = mount(
      <ConnectionQuotaPanel
        {...panelProps({
          cache: {
            ...panelProps().cache,
            bankedResetCredits: 3,
            paidCredits: { hasCredits: true, balance: 120, unlimited: false },
          },
        })}
      />
    );
    expect(codex.querySelectorAll('[role="progressbar"]')).toHaveLength(2);
    expect(codex.textContent).toContain("120 credits");
  });
});

describe("ConnectionRow quota gating", () => {
  const claudeCache: ProviderQuotaCacheEntry = {
    quotas: {
      "session (5h)": { used: 0, total: 0, remainingPercentage: 11, resetAt: futureReset(1) },
      "weekly (7d)": { used: 0, total: 0, remainingPercentage: 77, resetAt: futureReset(100) },
    },
    plan: "Claude Max",
    message: null,
    fetchedAt: new Date().toISOString(),
  };

  function rowProps(
    connection: ConnectionRowConnection,
    quotaCache?: ProviderQuotaCacheEntry | null
  ) {
    return {
      connection,
      isOAuth: true,
      isFirst: true,
      isLast: true,
      onMoveUp: () => {},
      onMoveDown: () => {},
      onToggleActive: () => {},
      onToggleRateLimit: () => {},
      onRetest: () => {},
      onEdit: () => {},
      onDelete: () => {},
      quotaCache,
      onRefreshQuota: vi.fn(),
    };
  }

  it("renders the quota strip and the plan badge for a supported provider", () => {
    const el = mount(
      React.createElement(ConnectionRow, {
        ...rowProps(
          { id: "a1", provider: "claude", name: "Claude", testStatus: "active" },
          claudeCache
        ),
      } as never)
    );
    expect(el.querySelectorAll('[role="progressbar"]')).toHaveLength(2);
    expect(el.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("11");
    expect(el.textContent).toContain("Claude Max");
  });

  it("hides the quota strip when the connection opted out (quotaVisible=false)", () => {
    const el = mount(
      React.createElement(ConnectionRow, {
        ...rowProps(
          {
            id: "a2",
            provider: "claude",
            name: "Claude hidden",
            testStatus: "active",
            quotaVisible: false,
          },
          claudeCache
        ),
      } as never)
    );
    expect(el.querySelectorAll('[role="progressbar"]')).toHaveLength(0);
    expect(el.textContent).not.toContain("Claude Max");
  });

  it("hides the quota strip for providers without a usage API", () => {
    const el = mount(
      React.createElement(ConnectionRow, {
        ...rowProps(
          { id: "a3", provider: "openai", name: "OpenAI", testStatus: "active" },
          claudeCache
        ),
      } as never)
    );
    expect(el.querySelectorAll('[role="progressbar"]')).toHaveLength(0);
  });
});
