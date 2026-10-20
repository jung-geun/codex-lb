import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { getModelSourceApiUsage, getModelSourceQuotaHistory, getModelSourceUsage, listModelSources, updateModelSource } from "@/features/model-sources/api";
import { server } from "@/test/mocks/server";

const interval = {
  sourceId: "source/team", windowStart: "2026-01-01T00:00:00Z", windowEnd: "2026-01-08T00:00:00Z",
};

describe("getModelSourceQuotaHistory", () => {
  it("requests seven days with an opaque source ID and preserves unknown observations", async () => {
    const requests: URL[] = [];
    const windows = [{
      windowKey: "seven_day", model: "claude-sonnet-4-6", usedRatio: null,
      limitValue: null, usedValue: null, resetsAt: null, observedAt: "2026-01-06T03:42:17Z",
    }];
    server.use(http.get("/api/model-sources/:sourceId/quota-history", ({ request }) => {
      requests.push(new URL(request.url));
      return HttpResponse.json({ ...interval, windows });
    }));

    const result = await getModelSourceQuotaHistory(interval.sourceId);

    expect(requests).toHaveLength(1);
    expect(requests[0].pathname).toBe("/api/model-sources/source%2Fteam/quota-history");
    expect(requests[0].search).toBe("?days=7");
    expect(result).toEqual({ ...interval, windows });
  });

  it("rejects missing history instead of presenting it as an empty seven-day chart", async () => {
    server.use(http.get("/api/model-sources/:sourceId/quota-history", () => HttpResponse.json(interval)));
    await expect(getModelSourceQuotaHistory(interval.sourceId)).rejects.toMatchObject({ code: "invalid_response_schema" });
  });

  it("preserves an absent endpoint error instead of converting it to empty history", async () => {
    server.use(http.get("/api/model-sources/:sourceId/quota-history", () => HttpResponse.json({
      error: { code: "not_found", message: "History is unavailable" },
    }, { status: 404 })));
    await expect(getModelSourceQuotaHistory(interval.sourceId)).rejects.toMatchObject({ status: 404, code: "not_found" });
  });
});

describe("getModelSourceUsage", () => {
  it.each([1, 7, 30] as const)("requests %s days and preserves unknown cost and incomplete recorded tokens", async (days) => {
    const requests: URL[] = [];
    const usage = { ...interval, requestCount: 2, successCount: 1, errorCount: 1, cancelledCount: 0, inputTokens: 15, outputTokens: 4, cachedInputTokens: 3, cacheCreationInputTokens: 2, reasoningTokens: 1, costUsd: null, knownCostUsd: 0.05, unpricedRequestCount: 1, unknownUsageCount: 1, lastRequestAt: null };
    server.use(http.get("/api/model-sources/:sourceId/usage", ({ request }) => {
      requests.push(new URL(request.url));
      return HttpResponse.json(usage);
    }));
    expect(await getModelSourceUsage(interval.sourceId, undefined, days)).toEqual(usage);
    expect(requests[0].pathname).toBe("/api/model-sources/source%2Fteam/usage");
    expect(requests[0].search).toBe(`?days=${days}`);
  });
});

describe("getModelSourceApiUsage", () => {
  it("retains native currency precision and distinct short-window token kinds", async () => {
    const payload = { sourceId: interval.sourceId, observedAt: "2026-01-01T00:00:00Z", rateLimits: [{ model: "claude-exact", tokenKind: "input_tokens", observedAt: "2026-01-01T00:00:00Z", tokenLimit: 10000, tokensRemaining: null, tokensResetAt: null, requestLimit: null, requestsRemaining: null, requestsResetAt: null }], balances: [{ currency: "CNY", totalBalance: "12.3400", grantedBalance: null, toppedUpBalance: "12.3400" }], balanceObservedAt: "2026-01-01T00:00:00Z", balanceStatus: "available", balanceError: null, balanceIsAvailable: true };
    server.use(http.get("/api/model-sources/:sourceId/api-usage", () => HttpResponse.json(payload)));
    expect(await getModelSourceApiUsage(interval.sourceId)).toEqual(payload);
  });

  it("rejects missing telemetry fields instead of manufacturing empty rates and zero balances", async () => {
    server.use(http.get("/api/model-sources/:sourceId/api-usage", () => HttpResponse.json({ sourceId: interval.sourceId })));
    await expect(getModelSourceApiUsage(interval.sourceId)).rejects.toMatchObject({ code: "invalid_response_schema" });
  });
});

describe("updateModelSource", () => {
  it("does not overwrite a saved fallback preference during unrelated source patches", async () => {
    const payloads: unknown[] = [];
    server.use(http.patch("/api/model-sources/:sourceId", async ({ request }) => {
      payloads.push(await request.json());
      return HttpResponse.json({ id: "api", name: "API", kind: "openai_compatible", baseUrl: "https://api.openai.com/v1", isEnabled: false, healthStatus: "healthy", supportsChatCompletions: true, supportsResponses: true, createdAt: "2026-01-01T00:00:00Z", updatedAt: "2026-01-01T00:00:00Z", apiFallbackOnly: false });
    }));
    const source = await updateModelSource("api", { isEnabled: false });
    expect(payloads).toEqual([{ isEnabled: false }]);
    expect(source.apiFallbackOnly).toBe(false);
  });
});

describe("listModelSources", () => {
  it("honors source permission-boundary cancellation instead of returning a list", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(listModelSources(controller.signal)).rejects.toBeDefined();
  });
});
