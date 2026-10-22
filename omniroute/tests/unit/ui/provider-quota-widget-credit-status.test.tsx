// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
vi.unmock("next-intl");
import { NextIntlClientProvider } from "next-intl";
import en from "@/i18n/messages/en.json";
import ko from "@/i18n/messages/ko.json";
import ProviderQuotaWidget from "@/app/(dashboard)/home/ProviderQuotaWidget";

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function renderCredits(provider: string, usage: unknown, locale: "en" | "ko" = "en") {
  vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => {
    const url = String(input);
    if (url === "/api/providers/client") {
      return Response.json({ connections: [{ id: "fixture", provider, authType: "oauth", name: "Offline fixture" }] });
    }
    if (url === "/api/usage/provider-limits") return Response.json({ caches: { fixture: usage } });
    throw new Error("Unexpected fixture request: " + url);
  }));
  await act(async () => root.render(
    <NextIntlClientProvider locale={locale} messages={locale === "en" ? en : ko} timeZone="UTC">
      <ProviderQuotaWidget />
    </NextIntlClientProvider>
  ));
}

function allowanceAmount(locale: "en" | "ko" = "en") {
  const label = (locale === "en" ? en : ko).providers.subscriptionCreditRemainingLimit;
  const element = Array.from(container.querySelectorAll("[title]"))
    .find((item) => item.getAttribute("title") === label);
  return element?.parentElement?.lastElementChild?.textContent;
}

it("keeps an unreported Claude amount unknown on the home dashboard", async () => {
  await renderCredits("claude", { quotas: {}, extraUsage: { is_enabled: true, monthly_limit: 6000, used_credits: 0 } }, "ko");
  expect(allowanceAmount("ko")).toBe(ko.providers.subscriptionCreditUnknown);
});

it("does not confuse a reported zero allowance with a missing amount", async () => {
  await renderCredits("claude", { quotas: {}, extraUsage: { is_enabled: true, monthly_limit: 0, used_credits: 0, decimal_places: 2 } });
  expect(allowanceAmount()).toBe("0 credits");
});

it("uses provider decimal units and currency for configured Claude allowance, not a wallet conversion", async () => {
  await renderCredits("claude", { quotas: {}, extraUsage: { is_enabled: true, monthly_limit: 6000, used_credits: 1234, decimal_places: 2, currency: "EUR" } });
  expect(allowanceAmount()).toBe("47.66 EUR");
});

it("does not round a small positive configured allowance into exhausted zero", async () => {
  await renderCredits("claude", { quotas: {}, extraUsage: { is_enabled: true, monthly_limit: 1, used_credits: 0, decimal_places: 6 } });
  expect(allowanceAmount()).toBe("0.000001 credits");
});
