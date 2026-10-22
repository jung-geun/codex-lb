// @vitest-environment jsdom
import React, { act, useEffect, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

vi.unmock("next-intl");
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "codex" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/dashboard/providers/codex",
}));
const notify = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };
vi.mock("@/store/notificationStore", () => ({ useNotificationStore: () => notify }));

import { NextIntlClientProvider } from "next-intl";
import en from "@/i18n/messages/en.json";
import ko from "@/i18n/messages/ko.json";
import ConnectionRow, { type ConnectionRowConnection } from "@/app/(dashboard)/dashboard/providers/[id]/components/ConnectionRow";
import ClaudeConnectionFields from "@/app/(dashboard)/dashboard/providers/[id]/components/modals/ClaudeConnectionFields";
import { useProviderConnections, type UseProviderConnectionsReturn } from "@/app/(dashboard)/dashboard/providers/[id]/hooks/useProviderConnections";
const messages = { en, ko };

describe("native subscription credit permission", () => {
  let container: HTMLDivElement;
  let root: Root;
  let hook: UseProviderConnectionsReturn;
  let accounts: ConnectionRowConnection[];
  let putStatus: number;
  let savedConnection: Partial<ConnectionRowConnection> | undefined;
  let networkFailure: boolean;
  let pendingPut: Promise<void> | null;
  let fetchMock: Mock;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    putStatus = 200;
    savedConnection = undefined;
    networkFailure = false;
    pendingPut = null;
    vi.clearAllMocks();
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        if (pendingPut) await pendingPut;
        if (networkFailure) throw new Error("offline");
        return { ok: putStatus === 200, json: async () => ({ error: "Update rejected", connection: savedConnection }) };
      }
      const url = String(input);
      const body = url.startsWith("/api/providers") ? { connections: accounts }
        : url === "/api/provider-nodes" ? { nodes: [] } : {};
      return { ok: true, json: async () => body, headers: { get: () => null } };
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function mount(provider: string, locale: "en" | "ko", optIn = false, errorSource = "rate_limit") {
    accounts = ["first", "second"].map((id, index) => ({
      id, provider, name: id, isActive: true,
      lastErrorSource: errorSource, lastError: "keep this diagnostic",
      testStatus: "unavailable", rateLimitedUntil: "2099-01-01T00:00:00Z",
      providerSpecificData: {
        workspaceId: id,
        ...(index === 0 && optIn ? provider === "claude"
          ? { blockExtraUsage: false } : { allowPaidCredits: true } : {}),
      },
    }));
    function Harness() {
      const result = useProviderConnections(provider, false, true);
      useEffect(() => { hook = result; }, [result]);
      return <>{result.connections.map((connection) => (
        <ConnectionRow key={connection.id} connection={connection} isOAuth
          isCodex={provider === "codex"} isClaude={provider === "claude" || provider === "antigravity"}
          isFirst isLast onMoveUp={() => {}} onMoveDown={() => {}}
          onToggleActive={() => {}} onToggleRateLimit={() => {}}
          onToggleCodexPaidCredits={(enabled) => result.handleToggleCodexPaidCredits(connection.id!, enabled)}
          onToggleClaudeExtraUsage={(blocked) => result.handleToggleClaudeExtraUsage(connection.id!, blocked!)}
          onRetest={() => {}} onEdit={() => {}} onDelete={() => {}} />
      ))}</>;
    }
    await act(async () => root.render(
      <NextIntlClientProvider locale={locale} messages={messages[locale]} timeZone="UTC">
        <Harness />
      </NextIntlClientProvider>
    ));
  }

  function control(id = "first") {
    return container.querySelector<HTMLButtonElement>(
      '[data-testid="subscription-credit-control-' + id + '"] button[role="switch"]'
    )!;
  }

  for (const locale of ["en", "ko"] as const) {
    for (const provider of ["codex", "claude"]) {
      it(provider + " saves independent account consent in both directions (" + locale + ")", async () => {
        await mount(provider, locale);
        expect(control().getAttribute("aria-checked")).toBe("false");
        expect(control().getAttribute("aria-label")).toBe(messages[locale].providers.subscriptionCreditAutoUseLabel);
        expect(container.textContent).toContain(messages[locale].providers.subscriptionCreditBoundary);
        for (const enabled of [true, false]) {
          await act(async () => control().click());
          expect(control().getAttribute("aria-checked")).toBe(String(enabled));
          const puts = fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT");
          expect(puts.at(-1)?.[0]).toBe("/api/providers/first");
          expect(JSON.parse(puts.at(-1)?.[1].body)).toEqual({
            providerSpecificData: provider === "claude"
              ? { blockExtraUsage: !enabled } : { allowPaidCredits: enabled },
          });
          expect(hook.connections[0]).toMatchObject({
            lastErrorSource: "rate_limit", lastError: "keep this diagnostic",
            testStatus: "unavailable", rateLimitedUntil: "2099-01-01T00:00:00Z",
            providerSpecificData: { workspaceId: "first" },
          });
          expect(hook.connections[1]).toEqual(accounts[1]);
          expect(control("second").getAttribute("aria-checked")).toBe("false");
        }
        await act(async () => control("second").click());
        expect(control("second").getAttribute("aria-checked")).toBe("true");
        expect(control().getAttribute("aria-checked")).toBe("false");
      });
    }
  }

  for (const provider of ["codex", "claude"]) {
    it(provider + " preserves existing explicit opt-in without writing on load", async () => {
      await mount(provider, "ko", true);
      expect(control().getAttribute("aria-checked")).toBe("true");
      expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(0);
    });

    for (const failure of ["rejected", "network"]) {
      it(provider + " leaves consent unchanged after " + failure + " failure", async () => {
        putStatus = failure === "rejected" ? 500 : 200;
        networkFailure = failure === "network";
        await mount(provider, "en");
        await act(async () => control().click());
        expect(control().getAttribute("aria-checked")).toBe("false");
        expect(hook.connections[0]).toEqual(accounts[0]);
        expect(notify.error).toHaveBeenCalled();
        expect(notify.success).not.toHaveBeenCalled();
      });
    }

    it(provider + " prevents conflicting writes while saving", async () => {
      await mount(provider, "en");
      const { promise, resolve: finish } = Promise.withResolvers<void>();
      pendingPut = promise;
      await act(async () => { control().click(); });
      expect(control().disabled).toBe(true);
      expect(control().getAttribute("aria-checked")).toBe("false");
      await act(async () => {
        if (provider === "codex") await hook.handleToggleCodexPaidCredits("first", false);
        else await hook.handleToggleClaudeExtraUsage("first", true);
      });
      expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
      await act(async () => { finish(); });
      expect(control().disabled).toBe(false);
      expect(control().getAttribute("aria-checked")).toBe("true");
    });
  }

  it("does not restore stale credit consent when a quota-policy save finishes after disable", async () => {
    await mount("codex", "en", true);
    const creditGate = Promise.withResolvers<void>();
    const policyGate = Promise.withResolvers<void>();
    const serverPsd = { ...accounts[0].providerSpecificData };
    fetchMock.mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const patch = JSON.parse(String(init?.body)).providerSpecificData as Record<string, unknown>;
      if (Object.hasOwn(patch, "codexLimitPolicy")) await policyGate.promise;
      else await creditGate.promise;
      Object.assign(serverPsd, patch);
      return { ok: true, json: async () => ({ connection: {
        ...accounts[0], providerSpecificData: { ...serverPsd },
      } }) };
    });
    await act(async () => { control().click(); });
    let policySave!: Promise<void>;
    await act(async () => { policySave = hook.handleToggleCodexLimit("first", "use5h", false); });
    const policyRequest = fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT").at(-1);
    const policyPatch = JSON.parse(policyRequest?.[1].body).providerSpecificData;
    expect(Object.keys(policyPatch)).toEqual(["codexLimitPolicy"]);
    await act(async () => { creditGate.resolve(); });
    expect(control().getAttribute("aria-checked")).toBe("false");
    await act(async () => { policyGate.resolve(); await policySave; });
    expect(serverPsd.allowPaidCredits).toBe(false);
    expect(hook.connections[0].providerSpecificData?.allowPaidCredits).toBe(false);
    expect(control().getAttribute("aria-checked")).toBe("false");
    expect(hook.connections[1]).toEqual(accounts[1]);
  });

  it("uses a server-confirmed local extra-usage restriction clear, never inventing one", async () => {
    await mount("claude", "ko", false, "extra_usage");
    savedConnection = { id: "first", testStatus: "active", lastErrorSource: null,
      lastError: null, rateLimitedUntil: null };
    await act(async () => control().click());
    expect(control().getAttribute("aria-checked")).toBe("true");
    expect(hook.connections[0].testStatus).toBe("active");
    expect(hook.connections[0].lastErrorSource).toBeNull();
    expect(hook.connections[0].rateLimitedUntil).toBeNull();
  });

  it("never exposes native Claude credit consent for Antigravity-hosted Claude", async () => {
    await mount("antigravity", "ko");
    expect(control()).toBeNull();
  });

  it("presents the inverse Claude flag as a positive permission in the edit fields", async () => {
    function Fields() {
      const [values, setValues] = useState({ blockExtraUsage: true, lowPriorityMode: false, autoLimitReset: false });
      return <ClaudeConnectionFields values={values} showUsageWallOptions
        onChange={(patch) => setValues((previous) => ({ ...previous, ...patch }))} />;
    }
    await act(async () => root.render(
      <NextIntlClientProvider locale="ko" messages={ko} timeZone="UTC"><Fields /></NextIntlClientProvider>
    ));
    const toggle = container.querySelector<HTMLButtonElement>('button[aria-label="구독 소진 후 크레딧 자동 사용"]')!;
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    await act(async () => toggle.click());
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    await act(async () => toggle.click());
    expect(toggle.getAttribute("aria-checked")).toBe("false");
  });
});
