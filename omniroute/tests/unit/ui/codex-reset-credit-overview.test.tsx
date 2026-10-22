// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
vi.unmock("next-intl");
import { NextIntlClientProvider } from "next-intl";
import en from "@/i18n/messages/en.json";
import ko from "@/i18n/messages/ko.json";
import CodexResetCreditOverview from "@/app/(dashboard)/dashboard/providers/[id]/components/CodexResetCreditOverview";
import ConnectionRow from "@/app/(dashboard)/dashboard/providers/[id]/components/ConnectionRow";

let root: Root | null = null;
let container: HTMLDivElement;
const NOW = Date.parse("2026-10-22T12:00:00Z");

beforeEach(() => {
  (
    globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(NOW);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function render(children: React.ReactNode, locale: "en" | "ko" = "en") {
  await act(async () =>
    root?.render(
      <NextIntlClientProvider locale={locale} messages={locale === "en" ? en : ko} timeZone="UTC">
        {children}
      </NextIntlClientProvider>
    )
  );
}

const overview = (connectionId = "account-a", enabled = true) => (
  <CodexResetCreditOverview connectionId={connectionId} enabled={enabled} />
);
const entryTexts = () =>
  Array.from(container.querySelectorAll('[data-testid="codex-reset-credit-entry"]')).map(
    (entry) => entry.textContent ?? ""
  );
const list = (credits: unknown[], availableCount: unknown = credits.length) =>
  Response.json({ ok: true, credits, availableCount });
const dateLabel = (timestamp: string, locale = "en") =>
  new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(new Date(timestamp));

it("loads metadata with GET on mount and renders sorted expiry, grant date and incomplete counts", async () => {
  const fetchMock = vi.fn(async () =>
    list(
      [
        { selectionToken: "never-show-token-later", expiresAt: "2026-10-25T12:00:00Z" },
        { selectionToken: "never-show-token-unknown", expiresAt: null },
        {
          selectionToken: "never-show-token-soon",
          expiresAt: "2026-10-22T13:00:00Z",
          grantedAt: "2026-10-21T08:00:00Z",
        },
      ],
      5
    )
  );
  vi.stubGlobal("fetch", fetchMock);
  await render(overview("account / a"));
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/usage/codex-reset-credit?connectionId=account%20%2F%20a");
  expect(options).toMatchObject({ method: "GET", cache: "no-store" });
  expect(options.body).toBeUndefined();
  expect(entryTexts()).toHaveLength(3);
  expect(entryTexts()[0]).toContain(dateLabel("2026-10-22T13:00:00Z"));
  expect(entryTexts()[0]).toContain(dateLabel("2026-10-21T08:00:00Z"));
  expect(entryTexts()[0]).toContain(en.providers.codexResetOverviewSoon);
  expect(entryTexts()[0]).toContain("remaining");
  expect(entryTexts()[1]).toContain(dateLabel("2026-10-25T12:00:00Z"));
  expect(entryTexts()[2]).toContain(en.providers.codexResetOverviewExpiryUnknown);
  expect(container.textContent).toContain("Provider-reported count at load: 5");
  expect(container.textContent).toContain("2 reported coupons have no individual details");
  expect(container.textContent).not.toContain("never-show-token");
  expect(container.querySelectorAll("button")).toHaveLength(0);
});

it("expires a coupon while the page stays open without claiming it remains redeemable or sending a request", async () => {
  const fetchMock = vi.fn(async () =>
    list([{ selectionToken: "secret", expiresAt: "2026-10-22T12:01:00Z" }])
  );
  vi.stubGlobal("fetch", fetchMock);
  await render(overview());
  expect(entryTexts()[0]).toContain("remaining");
  const countAtLoad = container.querySelector(
    '[data-testid="codex-reset-credit-count"]'
  )!.textContent;
  await act(async () => {
    vi.advanceTimersByTime(61_000);
  });
  expect(entryTexts()[0]).toContain(en.providers.codexResetOverviewExpired);
  expect(entryTexts()[0]).not.toContain("remaining");
  expect(entryTexts()[0]).not.toContain(en.providers.codexResetOverviewSoon);
  expect(container.querySelector('[data-testid="codex-reset-credit-count"]')!.textContent).toBe(
    countAtLoad
  );
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("treats missing, null and invalid expiry as unknown, and past expiry as expired", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      list([
        {},
        { expiresAt: null },
        { expiresAt: "not-a-date", grantedAt: "invalid" },
        { expiresAt: "2026-10-21T12:00:00Z" },
      ])
    )
  );
  await render(overview());
  expect(entryTexts()[0]).toContain(en.providers.codexResetOverviewExpired);
  expect(
    entryTexts()
      .slice(1)
      .every((text) => text.includes(en.providers.codexResetOverviewExpiryUnknown))
  ).toBe(true);
  expect(container.textContent).not.toContain("Invalid Date");
  expect(container.textContent).not.toContain("No expiry");
  expect(entryTexts().some((text) => text.includes("remaining"))).toBe(false);
});

it("renders localized absolute expiry and countdown in Korean", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      list([{ expiresAt: "2026-10-22T12:01:30Z", grantedAt: "2026-10-20T12:00:00Z" }])
    )
  );
  await render(overview(), "ko");
  expect(container.textContent).toContain(ko.providers.codexResetOverviewTitle);
  expect(entryTexts()[0]).toContain(dateLabel("2026-10-22T12:01:30Z", "ko"));
  expect(entryTexts()[0]).toContain("남음");
  expect(entryTexts()[0]).toContain(ko.providers.codexResetOverviewSoon);
});

it.each([
  ["HTTP error", () => new Response("{}", { status: 503 })],
  ["error envelope", () => Response.json({ ok: false, credits: [], availableCount: 0 })],
  ["invalid payload", () => Response.json({ ok: true })],
  ["invalid JSON", () => new Response("not-json")],
])("shows unavailable, not zero, after %s", async (_label, response) => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => response())
  );
  await render(overview());
  expect(container.textContent).toContain(en.providers.codexResetOverviewUnavailable);
  expect(container.querySelector('[data-testid="codex-reset-credit-count"]')).toBeNull();
  expect(entryTexts()).toHaveLength(0);
});

it("does not turn a missing count into a zero balance", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => list([{}], null))
  );
  await render(overview());
  expect(container.textContent).toContain(en.providers.codexResetOverviewCountUnknown);
  expect(entryTexts()).toHaveLength(1);
});

it("aborts the old account and ignores its late response after an ID change", async () => {
  const first = Promise.withResolvers<Response>();
  const second = Promise.withResolvers<Response>();
  const fetchMock = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  vi.stubGlobal("fetch", fetchMock);
  await render(overview("account-a"));
  expect(container.textContent).toContain(en.providers.codexResetOverviewLoading);
  await render(overview("account-b"));
  const oldOptions = fetchMock.mock.calls[0][1] as RequestInit;
  expect(oldOptions.signal?.aborted).toBe(true);
  await act(async () => {
    second.resolve(list([{ expiresAt: "2026-10-25T12:00:00Z" }]));
  });
  await act(async () => {
    first.resolve(list([{ expiresAt: "2026-10-23T12:00:00Z" }], 10));
  });
  expect(entryTexts()).toHaveLength(1);
  expect(entryTexts()[0]).toContain(dateLabel("2026-10-25T12:00:00Z"));
  expect(container.textContent).not.toContain(dateLabel("2026-10-23T12:00:00Z"));
  expect(container.textContent).not.toContain("at load: 10");
});

it("aborts on unmount and does not share a previous mount's late result", async () => {
  const old = Promise.withResolvers<Response>();
  const current = Promise.withResolvers<Response>();
  const fetchMock = vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise);
  vi.stubGlobal("fetch", fetchMock);
  await render(overview());
  act(() => root?.unmount());
  root = null;
  expect((fetchMock.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true);
  root = createRoot(container);
  await render(overview());
  await act(async () => {
    old.resolve(list([{}, {}], 2));
  });
  expect(container.textContent).toContain(en.providers.codexResetOverviewLoading);
  await act(async () => {
    current.resolve(list([], 0));
  });
  expect(entryTexts()).toHaveLength(0);
  expect(container.textContent).toContain("at load: 0");
});

it("handles StrictMode's cancelled mount without a shared cache race", async () => {
  const old = Promise.withResolvers<Response>();
  const current = Promise.withResolvers<Response>();
  const fetchMock = vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise);
  vi.stubGlobal("fetch", fetchMock);
  await act(async () =>
    root?.render(
      <React.StrictMode>
        <NextIntlClientProvider locale="en" messages={en} timeZone="UTC">
          {overview()}
        </NextIntlClientProvider>
      </React.StrictMode>
    )
  );
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect((fetchMock.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true);
  await act(async () => {
    current.resolve(list([{ expiresAt: null }], 1));
  });
  await act(async () => {
    old.resolve(list([], 0));
  });
  expect(entryTexts()).toHaveLength(1);
  expect(container.textContent).toContain("at load: 1");
});

const nativeRowProps = {
  connection: {
    id: "native-account",
    provider: "codex",
    name: "Codex account",
    isActive: true,
    testStatus: "active",
    priority: 1,
    quotaVisible: false,
  },
  isOAuth: true,
  isCodex: true,
  isFirst: true,
  isLast: true,
  onMoveUp: () => {},
  onMoveDown: () => {},
  onToggleActive: () => {},
  onToggleRateLimit: () => {},
  onRetest: () => {},
  onEdit: () => {},
  onDelete: () => {},
};

it("mounts on the native Codex default row even with quota hidden and no consent-control callback", async () => {
  const fetchMock = vi.fn(async () => list([{ expiresAt: "2026-10-23T12:00:00Z" }]));
  vi.stubGlobal("fetch", fetchMock);
  await render(<ConnectionRow {...nativeRowProps} />);
  expect(container.querySelector('[data-testid="codex-reset-credit-overview"]')).not.toBeNull();
  expect(entryTexts()[0]).toContain(dateLabel("2026-10-23T12:00:00Z"));
  expect(
    fetchMock.mock.calls.every((args) => ((args as unknown[])[1] as RequestInit)?.method === "GET")
  ).toBe(true);
});

it("does not probe disabled native accounts or mount on non-native Codex rows", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  await render(
    <ConnectionRow
      {...nativeRowProps}
      connection={{ ...nativeRowProps.connection, isActive: false }}
    />
  );
  expect(container.textContent).toContain(en.providers.codexResetOverviewUnavailable);
  expect(fetchMock).not.toHaveBeenCalled();
  await render(<ConnectionRow {...nativeRowProps} isOAuth={false} />);
  expect(container.querySelector('[data-testid="codex-reset-credit-overview"]')).toBeNull();
  expect(fetchMock).not.toHaveBeenCalled();
  await render(
    <ConnectionRow
      {...nativeRowProps}
      connection={{ ...nativeRowProps.connection, provider: "openai" }}
    />
  );
  expect(container.querySelector('[data-testid="codex-reset-credit-overview"]')).toBeNull();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("keeps simultaneously mounted accounts isolated when responses arrive out of order", async () => {
  const first = Promise.withResolvers<Response>();
  const second = Promise.withResolvers<Response>();
  const fetchMock = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  vi.stubGlobal("fetch", fetchMock);
  await render(
    <>
      {overview("account-a")}
      {overview("account-b")}
    </>
  );
  await act(async () => {
    second.resolve(list([{ expiresAt: "2026-10-25T12:00:00Z" }]));
  });
  await act(async () => {
    first.resolve(list([], 0));
  });
  const panels = container.querySelectorAll('[data-testid="codex-reset-credit-overview"]');
  expect(panels).toHaveLength(2);
  expect(panels[0].textContent).toContain("at load: 0");
  expect(panels[0].querySelectorAll('[data-testid="codex-reset-credit-entry"]')).toHaveLength(0);
  expect(panels[1].querySelectorAll('[data-testid="codex-reset-credit-entry"]')).toHaveLength(1);
  expect(panels[1].textContent).toContain(dateLabel("2026-10-25T12:00:00Z"));
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it("reports a network failure as unavailable without exposing the error or retrying", async () => {
  const fetchMock = vi.fn().mockRejectedValue(new Error("private-provider-error"));
  vi.stubGlobal("fetch", fetchMock);
  await render(overview());
  expect(container.textContent).toContain(en.providers.codexResetOverviewUnavailable);
  expect(container.textContent).not.toContain("private-provider-error");
  expect(container.querySelector('[data-testid="codex-reset-credit-count"]')).toBeNull();
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
