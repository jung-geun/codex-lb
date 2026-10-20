import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { get } from "@/lib/api-client";
import { useReports, useReportsOptions, useReportsSourceOptions, useThreadIdentity } from "./use-reports";

vi.mock("@/lib/api-client", () => ({ get: vi.fn().mockResolvedValue({}) }));
const getMock = vi.mocked(get);
const filters = {
  startDate: "2026-10-01", endDate: "2026-10-07", accountId: ["source:claude-1", "codex-1"],
  accountType: ["claude", "codex"], apiKeyId: ["key-1"], model: "claude-sonnet-4-5", useragent: "CLI",
};
function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  const wrapper = ({ children }: PropsWithChildren) => createElement(QueryClientProvider, { client }, children);
  return { client, wrapper };
}

beforeEach(() => getMock.mockClear());

describe("report source consumers", () => {
  it("sends repeated source/type and API-key filters to reports, options, and thread identity", async () => {
    const { client, wrapper } = setup();
    const { result, unmount } = renderHook(() => ({
      report: useReports(filters, "Asia/Seoul"),
      options: useReportsOptions(filters, "Asia/Seoul"),
      identity: useThreadIdentity(filters, "Asia/Seoul", true),
    }), { wrapper });
    await waitFor(() => expect(result.current.identity.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledTimes(3);
    for (const [rawUrl] of getMock.mock.calls) {
      const url = new URL(rawUrl, "http://localhost");
      expect(url.searchParams.getAll("account_id").sort()).toEqual([...filters.accountId].sort());
      expect(url.searchParams.getAll("account_type").sort()).toEqual(["claude", "codex"]);
      expect(url.searchParams.getAll("api_key_id")).toEqual(["key-1"]);
      expect(url.searchParams.get("start_date")).toBe(filters.startDate);
      expect(url.searchParams.get("end_date")).toBe(filters.endDate);
      expect(url.searchParams.get("timezone")).toBe("Asia/Seoul");
      if (url.pathname.endsWith("/options")) {
        expect(url.searchParams.has("model")).toBe(false);
        expect(url.searchParams.has("useragent_group")).toBe(false);
      } else {
        expect(url.searchParams.get("model")).toBe(filters.model);
        expect(url.searchParams.get("useragent_group")).toBe("CLI");
      }
    }
    for (const query of client.getQueryCache().getAll()) {
      expect(JSON.stringify(query.queryKey)).toContain("claude");
      expect(JSON.stringify(query.queryKey)).toContain("source:claude-1");
    }
    unmount();
    client.clear();
  });

  it.each(["accountId", "accountType"] as const)("isolates all endpoint caches when %s changes", async (field) => {
    const { client, wrapper } = setup();
    const { result, rerender, unmount } = renderHook(({ selection }) => {
      const selected = { ...filters, [field]: selection };
      return {
        report: useReports(selected, "UTC"),
        options: useReportsOptions(selected, "UTC"),
        identity: useThreadIdentity(selected, "UTC", true),
      };
    }, { wrapper, initialProps: { selection: ["claude"] } });
    await waitFor(() => expect(result.current.identity.isSuccess).toBe(true));
    rerender({ selection: ["codex"] });
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(6));
    expect(client.getQueryCache().getAll()).toHaveLength(6);
    for (const [rawUrl] of getMock.mock.calls.slice(3)) {
      expect(new URL(rawUrl, "http://localhost").searchParams.getAll(field === "accountType" ? "account_type" : "account_id")).toEqual(["codex"]);
    }
    unmount();
    client.clear();
  });

  it.each([
    { apiKeyId: ["key-2"] }, { model: "claude-opus-4-6" }, { useragent: "SDK" },
    { startDate: "2026-10-02" }, { endDate: "2026-10-06" },
  ])("isolates thread identity for combined filter change %j", async (change) => {
    const { client, wrapper } = setup();
    const { result, rerender, unmount } = renderHook(({ scope }) => useThreadIdentity(scope, "UTC", true), {
      wrapper, initialProps: { scope: filters },
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    rerender({ scope: { ...filters, ...change } });
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2));
    expect(client.getQueryCache().getAll()).toHaveLength(2);
    unmount();
    client.clear();
  });

  it("retains date-only thread callers, timezone isolation, and the visibility gate", async () => {
    const { client, wrapper } = setup();
    const { result, rerender, unmount } = renderHook(({ enabled, timezone }) => useThreadIdentity({
      startDate: filters.startDate, endDate: filters.endDate,
    }, timezone, enabled), { wrapper, initialProps: { enabled: false, timezone: "UTC" } });
    expect(getMock).not.toHaveBeenCalled();
    rerender({ enabled: true, timezone: "UTC" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = new URL(getMock.mock.calls[0]![0], "http://localhost");
    expect(url.searchParams.has("account_type")).toBe(false);
    expect(url.searchParams.has("account_id")).toBe(false);
    rerender({ enabled: true, timezone: "Asia/Seoul" });
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2));
    unmount();
    client.clear();
  });
  it("keeps source picker alternatives cached across account/type changes while preserving other filters", async () => {
    const { client, wrapper } = setup();
    const { result, rerender, unmount } = renderHook(({ scope }) => useReportsSourceOptions(scope, "Asia/Seoul"), {
      wrapper, initialProps: { scope: filters },
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const params = new URL(getMock.mock.calls[0]![0], "http://localhost").searchParams;
    expect(params.has("account_id")).toBe(false);
    expect(params.has("account_type")).toBe(false);
    expect(params.getAll("api_key_id")).toEqual(["key-1"]);
    expect(params.get("model")).toBe(filters.model);
    expect(params.get("useragent_group")).toBe("CLI");
    expect(params.get("timezone")).toBe("Asia/Seoul");
    rerender({ scope: { ...filters, accountId: ["codex-2"], accountType: ["codex"] } });
    expect(getMock).toHaveBeenCalledTimes(1);
    expect(client.getQueryCache().getAll()).toHaveLength(1);
    unmount();
    client.clear();
  });
});
