// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
vi.unmock("next-intl");
import { createTranslator, NextIntlClientProvider } from "next-intl";
import en from "@/i18n/messages/en.json";
import ko from "@/i18n/messages/ko.json";
import CodexCreditLimitFields from "@/app/(dashboard)/dashboard/providers/[id]/components/CodexCreditLimitFields";

let root: Root;
let container: HTMLDivElement;
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });

async function mount(props: Partial<React.ComponentProps<typeof CodexCreditLimitFields>>, locale: "en" | "ko" = "en") {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root.render(
    <NextIntlClientProvider locale={locale} messages={locale === "en" ? en : ko} timeZone="UTC">
      <CodexCreditLimitFields connectionId="acct" providerSpecificData={{}} {...props} />
    </NextIntlClientProvider>
  ));
}

async function type(index: number, value: string) {
  const input = container.querySelectorAll("input")[index] as HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  await act(async () => { setter.call(input, value); input.dispatchEvent(new Event("input", { bubbles: true })); });
}
const save = () => act(async () => { (container.querySelector("button") as HTMLButtonElement).click(); });

const locales = ["en", "ko"] as const;
const translate = (locale: "en" | "ko") => createTranslator({
  locale,
  messages: locale === "en" ? en : ko,
  namespace: "providers",
});
const usage = (window: "daily" | "weekly") =>
  container.querySelector(`[data-testid="codex-credit-${window}-usage"]`)!;
const progress = (window: "daily" | "weekly") =>
  usage(window).querySelector('[role="progressbar"]') as HTMLElement | null;
const width = (window: "daily" | "weekly") =>
  (progress(window)!.firstElementChild as HTMLElement).style.width;

it.each(locales)("shows both saved limit graphs with independent denominators in %s", async (locale) => {
  await mount({
    providerSpecificData: { creditDailyLimit: 200, creditWeeklyLimit: 1000 },
    creditSpend: { day: 50, week: 700, trackedSince: 0 },
  }, locale);
  const t = translate(locale);
  expect(container.querySelector('[data-testid="codex-credit-spend"]')).not.toBeNull();
  expect(container.querySelectorAll('[role="progressbar"]')).toHaveLength(2);
  expect(progress("daily")!.getAttribute("aria-label")).toBe(t("codexCreditWindowDaily"));
  expect(progress("weekly")!.getAttribute("aria-label")).toBe(t("codexCreditWindowWeekly"));
  expect(progress("daily")!.getAttribute("aria-valuemax")).toBe("200");
  expect(progress("daily")!.getAttribute("aria-valuenow")).toBe("50");
  expect(progress("weekly")!.getAttribute("aria-valuemax")).toBe("1000");
  expect(progress("weekly")!.getAttribute("aria-valuenow")).toBe("700");
  expect(width("daily")).toBe("25%");
  expect(width("weekly")).toBe("70%");
  expect(usage("daily").textContent).toContain(t("codexCreditUsagePercent", { percent: "25" }));
  expect(usage("daily").textContent).toContain(t("codexCreditUsageRemaining", { remaining: "150" }));
  expect(usage("weekly").textContent).toContain(t("codexCreditUsageRemaining", { remaining: "300" }));
});

it.each(locales)("represents observed zero as zero rather than unknown in %s", async (locale) => {
  await mount({
    providerSpecificData: { creditDailyLimit: 100, creditWeeklyLimit: 300 },
    creditSpend: { day: 0, week: 0, trackedSince: 0 },
  }, locale);
  const t = translate(locale);
  for (const window of ["daily", "weekly"] as const) {
    expect(progress(window)!.getAttribute("aria-valuenow")).toBe("0");
    expect(width(window)).toBe("0%");
    expect(usage(window).textContent).toContain(t("codexCreditUsagePercent", { percent: "0" }));
    expect(usage(window).textContent).not.toContain(t("codexCreditUsageUnavailable"));
  }
  expect(usage("daily").textContent).toContain(t("codexCreditUsageRemaining", { remaining: "100" }));
  expect(usage("weekly").textContent).toContain(t("codexCreditUsageRemaining", { remaining: "300" }));
});

it.each(locales.flatMap((locale) => [null, NaN, Infinity, -1].map((amount) => ({ locale, amount }))))(
  "does not turn an unknown/invalid $amount observation into zero in $locale",
  async ({ locale, amount }) => {
    await mount({
      providerSpecificData: { creditDailyLimit: 200, creditWeeklyLimit: 400 },
      creditSpend: amount === null ? null : { day: amount, week: 100, trackedSince: 0 },
    }, locale);
    expect(progress("daily")).toBeNull();
    expect(usage("daily").querySelector("[style]")).toBeNull();
    expect(usage("daily").textContent).toContain(translate(locale)("codexCreditUsageUnavailable"));
    expect(usage("daily").textContent).not.toMatch(/%/);
    if (amount === null) {
      expect(progress("weekly")).toBeNull();
    } else {
      expect(width("weekly")).toBe("25%");
    }
  }
);

it.each(locales)("shows uncapped observations without an invented percentage in %s", async (locale) => {
  await mount({
    providerSpecificData: { creditDailyLimit: 0 },
    creditSpend: { day: 0, week: 23, trackedSince: 0 },
  }, locale);
  const t = translate(locale);
  for (const window of ["daily", "weekly"] as const) {
    expect(progress(window)).toBeNull();
    expect(usage(window).querySelector("[style]")).toBeNull();
    expect(usage(window).textContent).not.toMatch(/%/);
    expect(usage(window).textContent).toContain(t("codexCreditUsageUncapped"));
  }
  expect(usage("weekly").textContent).toContain(t("codexCreditSpent", {
    window: t("codexCreditWindowWeekly"), spent: "23", limit: t("codexCreditNoLimit"),
  }));
});

it.each(locales)("labels real overage while clamping only the visual bar in %s", async (locale) => {
  await mount({
    providerSpecificData: { creditDailyLimit: 100, creditWeeklyLimit: 200 },
    creditSpend: { day: 250, week: 150, trackedSince: 0 },
  }, locale);
  const t = translate(locale);
  expect(width("daily")).toBe("100%");
  expect(progress("daily")!.getAttribute("aria-valuenow")).toBe("100");
  expect(progress("daily")!.getAttribute("aria-valuetext"))
    .toContain(t("codexCreditUsagePercent", { percent: "250" }));
  expect(usage("daily").textContent).toContain(t("codexCreditUsageOverLimit", { over: "150" }));
  expect(width("weekly")).toBe("75%");
  expect(usage("weekly").textContent).toContain(t("codexCreditUsageRemaining", { remaining: "50" }));
});

it.each(locales)("marks the saved limit as reached at exactly 100 percent in %s", async (locale) => {
  await mount({
    providerSpecificData: { creditDailyLimit: 100, creditWeeklyLimit: 400 },
    creditSpend: { day: 100, week: 400, trackedSince: 0 },
  }, locale);
  const t = translate(locale);
  for (const window of ["daily", "weekly"] as const) {
    expect(width(window)).toBe("100%");
    expect(progress(window)!.getAttribute("aria-valuenow"))
      .toBe(progress(window)!.getAttribute("aria-valuemax"));
    expect(progress(window)!.getAttribute("aria-valuetext"))
      .toContain(t("codexCreditUsageLimitReached"));
    expect(usage(window).textContent).toContain(t("codexCreditUsagePercent", { percent: "100" }));
    expect(usage(window).textContent).not.toContain(t("codexCreditUsageOverLimit", { over: "0" }));
  }
});

it.each(locales)("does not round a small positive observation or percentage to zero in %s", async (locale) => {
  await mount({
    providerSpecificData: { creditDailyLimit: 1, creditWeeklyLimit: 0.000004 },
    creditSpend: { day: 1e-9, week: 0.000002, trackedSince: 0 },
  }, locale);
  const t = translate(locale);
  const scientific = new Intl.NumberFormat(locale, {
    maximumSignificantDigits: 8, notation: "scientific",
  });
  expect(Number(progress("daily")!.getAttribute("aria-valuenow"))).toBe(1e-9);
  expect(parseFloat(width("daily"))).toBeGreaterThan(0);
  expect(usage("daily").textContent).toContain(scientific.format(1e-9));
  expect(usage("daily").textContent)
    .toContain(t("codexCreditUsagePercent", { percent: scientific.format(1e-7) }));
  expect(width("weekly")).toBe("50%");
});

it.each(locales)("uses applied limits until a successful save, including while pending, in %s", async (locale) => {
  const { promise, resolve: finishSave } = Promise.withResolvers<Response>();
  const fetchMock = vi.fn(() => promise);
  vi.stubGlobal("fetch", fetchMock);
  await mount({
    providerSpecificData: { allowPaidCredits: true, creditDailyLimit: 200, creditWeeklyLimit: 400 },
    creditSpend: { day: 50, week: 100, trackedSince: 0 },
  }, locale);
  await type(0, "100");
  await type(1, "800");
  expect(width("daily")).toBe("25%");
  expect(width("weekly")).toBe("25%");
  await save();
  expect(width("daily")).toBe("25%");
  expect(width("weekly")).toBe("25%");
  expect((container.querySelector("button") as HTMLButtonElement).disabled).toBe(true);
  await act(async () => { finishSave(Response.json({ connection: { id: "acct" } })); });
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/providers/acct");
  expect(options.method).toBe("PUT");
  expect(JSON.parse(String(options.body))).toEqual({
    providerSpecificData: { creditDailyLimit: 100, creditWeeklyLimit: 800 },
  });
  expect(width("daily")).toBe("50%");
  expect(width("weekly")).toBe("12.5%");
  expect(progress("daily")!.getAttribute("aria-valuemax")).toBe("100");
  expect(progress("weekly")!.getAttribute("aria-valuemax")).toBe("800");
});

it("clears a saved cap with null without turning uncapped usage into a percentage", async () => {
  const fetchMock = vi.fn(async () => Response.json({ connection: { id: "acct" } }));
  vi.stubGlobal("fetch", fetchMock);
  await mount({
    providerSpecificData: { creditDailyLimit: 200, creditWeeklyLimit: 1000 },
    creditSpend: { day: 50, week: 100, trackedSince: 0 },
  });
  await type(1, "");
  expect(width("weekly")).toBe("10%");
  await save();
  const options = (fetchMock.mock.calls[0] as unknown[])[1] as RequestInit;
  expect(JSON.parse(String(options.body))).toEqual({
    providerSpecificData: { creditDailyLimit: 200, creditWeeklyLimit: null },
  });
  expect(progress("weekly")).toBeNull();
  expect(usage("weekly").textContent).not.toMatch(/%/);
  expect(width("daily")).toBe("25%");
});

it("rejects a non-positive draft without changing the saved graph or sending a request", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  await mount({
    providerSpecificData: { creditDailyLimit: 100 },
    creditSpend: { day: 20, week: 20, trackedSince: 0 },
  });
  await type(0, "0");
  await save();
  expect(fetchMock).not.toHaveBeenCalled();
  expect(width("daily")).toBe("20%");
  expect(progress("daily")!.getAttribute("aria-valuemax")).toBe("100");
});

it.each(locales)("keeps both applied limits after a failed save in %s", async (locale) => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
  await mount({
    providerSpecificData: { creditDailyLimit: 200, creditWeeklyLimit: 400 },
    creditSpend: { day: 10, week: 100, trackedSince: 0 },
  }, locale);
  await type(0, "900");
  await type(1, "800");
  await save();
  expect(container.textContent).toContain(translate(locale)("codexCreditLimitFailed"));
  expect(progress("daily")!.getAttribute("aria-valuemax")).toBe("200");
  expect(progress("weekly")!.getAttribute("aria-valuemax")).toBe("400");
  expect(width("daily")).toBe("5%");
  expect(width("weekly")).toBe("25%");
});

it.each(locales)("keeps the recent tracker lower-bound disclosure beside the graphs in %s", async (locale) => {
  await mount({
    providerSpecificData: { creditDailyLimit: 200, creditWeeklyLimit: 400 },
    creditSpend: { day: 1, week: 1, trackedSince: Date.now() - 1000 },
  }, locale);
  expect(container.querySelector('[data-testid="codex-credit-spend"]')!.textContent)
    .toContain(translate(locale)("codexCreditSpendLowerBound"));
  expect(container.querySelectorAll('[role="progressbar"]')).toHaveLength(2);
});
