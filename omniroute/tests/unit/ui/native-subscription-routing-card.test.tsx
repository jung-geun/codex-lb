// @vitest-environment jsdom
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import en from "../../../src/i18n/messages/en.json";
import ko from "../../../src/i18n/messages/ko.json";
import NativeSubscriptionRoutingCard from "../../../src/app/(dashboard)/dashboard/settings/components/NativeSubscriptionRoutingCard";

// Exercise locale context instead of the suite's English-only translation mock.
vi.unmock("next-intl");

const policy = { enabled: false, subscriptionOnlyKeyIds: ["existing"] };
const keys = [{ id: "existing", name: "Existing key" }, { id: "second", name: "Second key" }];

function card(locale: "en" | "ko") {
  return (
    <NextIntlClientProvider locale={locale} messages={locale === "ko" ? ko : en}>
      <NativeSubscriptionRoutingCard />
    </NextIntlClientProvider>
  );
}

function setup(options: { failLoad?: boolean; failSave?: boolean } = {}) {
  const writes: Record<string, unknown>[] = [];
  let failLoad = options.failLoad;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "PATCH") {
      const payload = JSON.parse(String(init.body));
      writes.push(payload);
      return { ok: !options.failSave, json: async () => payload };
    }
    return {
      ok: !failLoad,
      json: async () => url === "/api/keys"
        ? { keys }
        : { nativeSubscriptionRouting: policy, fallbackStrategy: "random" },
    };
  });
  vi.stubGlobal("fetch", fetchMock);
  return { writes, fetchMock, recoverLoad: () => { failLoad = false; } };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("native subscription routing localization", () => {
  it("keeps unsaved policy and password across locale changes without reloading settings", async () => {
    const { writes } = setup();
    const view = render(card("en"));
    const secondKey = await screen.findByRole("switch", { name: "Second key" });
    expect(screen.getByRole("switch", { name: en.settings.nativeRoutingAllowPaidFallback }).getAttribute("aria-checked")).toBe("false");
    fireEvent.click(secondKey);
    fireEvent.change(screen.getByLabelText(en.settings.nativeRoutingCurrentPassword), {
      target: { value: "current-secret" },
    });

    view.rerender(card("ko"));
    expect(screen.getByRole("switch", { name: "Second key" }).getAttribute("aria-checked")).toBe("true");
    expect((screen.getByLabelText(ko.settings.nativeRoutingCurrentPassword) as HTMLInputElement).value).toBe("current-secret");
    fireEvent.click(screen.getByRole("button", { name: ko.settings.nativeRoutingSave }));
    await screen.findByRole("status");
    expect(writes).toEqual([{
      nativeSubscriptionRouting: { enabled: false, subscriptionOnlyKeyIds: ["existing", "second"] },
      currentPassword: "current-secret",
    }]);
    expect((screen.getByLabelText(ko.settings.nativeRoutingCurrentPassword) as HTMLInputElement).value).toBe("");

    view.rerender(card("en"));
  });

  it("localizes a retained load error and retries without enabling writes before recovery", async () => {
    const { recoverLoad } = setup({ failLoad: true });
    const view = render(card("en"));
    await screen.findByRole("alert");
    view.rerender(card("ko"));
    expect((screen.getByRole("switch", { name: ko.settings.nativeRoutingAllowPaidFallback }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: ko.settings.nativeRoutingSave }) as HTMLButtonElement).disabled).toBe(true);

    recoverLoad();
    fireEvent.click(screen.getByRole("button", { name: ko.settings.nativeRoutingRetry }));
    await screen.findByRole("switch", { name: "Existing key" });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("clears the password on save failure while preserving the draft and localized alert", async () => {
    const { writes } = setup({ failSave: true });
    const view = render(card("ko"));
    await screen.findByRole("switch", { name: "Existing key" });
    fireEvent.click(screen.getByRole("switch", { name: ko.settings.nativeRoutingAllowPaidFallback }));
    fireEvent.change(screen.getByLabelText(ko.settings.nativeRoutingCurrentPassword), {
      target: { value: "current-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: ko.settings.nativeRoutingSave }));
    await screen.findByRole("alert");
    expect(writes).toEqual([{
      nativeSubscriptionRouting: { enabled: true, subscriptionOnlyKeyIds: ["existing"] },
      currentPassword: "current-secret",
    }]);
    expect((screen.getByLabelText(ko.settings.nativeRoutingCurrentPassword) as HTMLInputElement).value).toBe("");
    expect(screen.getByRole("switch", { name: ko.settings.nativeRoutingAllowPaidFallback }).getAttribute("aria-checked")).toBe("true");
    expect((screen.getByRole("button", { name: ko.settings.nativeRoutingSave }) as HTMLButtonElement).disabled).toBe(true);
    view.rerender(card("en"));
    expect(writes).toHaveLength(1);
  });
});
