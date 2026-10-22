// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
vi.unmock("next-intl");
import { NextIntlClientProvider } from "next-intl";
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

it("saves only the two limit settings and clears an empty field with null", async () => {
  const fetchMock = vi.fn(async () => Response.json({ connection: { id: "acct" } }));
  vi.stubGlobal("fetch", fetchMock);
  await mount({ providerSpecificData: { allowPaidCredits: true, creditWeeklyLimit: 3000 },
    creditSpend: { day: 120.5, week: 900, trackedSince: 0 } });
  await type(0, "500");
  await type(1, "");
  await save();
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(JSON.parse(String((fetchMock.mock.calls[0] as unknown[])[1] && ((fetchMock.mock.calls[0] as unknown[])[1] as RequestInit).body)))
    .toEqual({ providerSpecificData: { creditDailyLimit: 500, creditWeeklyLimit: null } });
  const spend = container.querySelector('[data-testid="codex-credit-spend"]')!.textContent!;
  expect(spend).toContain("120.5 used / limit 500");
  expect(spend).toContain(`900 used / limit ${en.providers.codexCreditNoLimit}`);
  expect(container.textContent).toContain(en.providers.codexCreditLimitSaved);
});

it("rejects a non-positive limit without sending a request", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  await mount({});
  await type(0, "0");
  await save();
  expect(fetchMock).not.toHaveBeenCalled();
  expect(container.textContent).toContain(en.providers.codexCreditLimitInvalid);
});

it("keeps the previous limits when the server refuses the save", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
  await mount({ providerSpecificData: { creditDailyLimit: 200 }, creditSpend: { day: 10, week: 10, trackedSince: 0 } }, "ko");
  await type(0, "900");
  await save();
  expect(container.textContent).toContain(ko.providers.codexCreditLimitFailed);
  expect(container.querySelector('[data-testid="codex-credit-spend"]')!.textContent).toContain("한도 200");
});

it("does not invent consumption before the first balance observation", async () => {
  await mount({ providerSpecificData: { creditDailyLimit: 200 }, creditSpend: null });
  expect(container.querySelector('[data-testid="codex-credit-spend"]')!.textContent)
    .toContain(`${en.providers.subscriptionCreditUnknown} used`);
});
