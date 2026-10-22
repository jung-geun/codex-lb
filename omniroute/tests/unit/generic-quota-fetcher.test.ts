import test, { mock } from "node:test";
import assert from "node:assert/strict";
import { setImmediate as waitImmediate } from "node:timers/promises";
import { _resetClaudeUsagePollingState } from "../../open-sse/services/usage/claude.ts";
import {
  _resetClaudeOauthUsageCooldown,
  isClaudeOauthUsageCoolingDown,
} from "../../open-sse/services/claudeUsageCooldown.ts";
import { normalizeClaudeUsageQuotas } from "../../open-sse/services/usage/claudeQuota.ts";

const genericModule = await import("../../open-sse/services/genericQuotaFetcher.ts");
const preflightModule = await import("../../open-sse/services/quotaPreflight.ts");
const originalFetch = globalThis.fetch;

const {
  convertUsageToQuotaInfo,
  fetchGenericQuota,
  invalidateGenericQuotaCache,
  invalidateQuotaCacheOnStatus,
  registerGenericQuotaFetchers,
  __setGenericUsageFetcherForTests,
  __agePendingForceRefreshForTests,
  __agePendingForceRefreshMissForTests,
  __resetGenericQuotaFetcherForTests,
} = genericModule;
const { getQuotaFetcher } = preflightModule;

function usageShape(remainingPercentage: number) {
  return {
    quotas: {
      "gemini-3-flash": {
        remainingPercentage,
        fractionReported: true,
        resetAt: "2026-09-01T20:00:00Z",
      },
      gemini_models_weekly: {
        remainingPercentage,
        fractionReported: true,
        resetAt: "2026-09-07T00:00:00Z",
      },
    },
  };
}

test("convertUsageToQuotaInfo returns null on null/undefined input", () => {
  assert.equal(convertUsageToQuotaInfo(null), null);
  assert.equal(convertUsageToQuotaInfo(undefined), null);
});

test("convertUsageToQuotaInfo returns null when only an error message is present", () => {
  // Auth-expired-style response from getUsageForProvider — fail open.
  assert.equal(convertUsageToQuotaInfo({ message: "auth expired" }), null);
});

test("convertUsageToQuotaInfo maps remainingPercentage into per-window percentUsed", () => {
  const result = convertUsageToQuotaInfo({
    quotas: {
      session: { remainingPercentage: 30, resetAt: "2026-05-14T20:00:00Z" },
      weekly: { remainingPercentage: 10, resetAt: "2026-05-21T00:00:00Z" },
    },
  });
  assert.ok(result);
  assert.deepEqual(result!.windows, {
    session: { percentUsed: 0.7, resetAt: "2026-05-14T20:00:00Z" },
    weekly: { percentUsed: 0.9, resetAt: "2026-05-21T00:00:00Z" },
  });
  // Worst-case percentUsed mirrors what the legacy single-signal field needs.
  assert.equal(result!.percentUsed, 0.9);
  // Reset time should track the worst-case window so preflight can surface it.
  assert.equal(result!.resetAt, "2026-05-21T00:00:00Z");
});

test("convertUsageToQuotaInfo maps Kimi Coding code_5h/code_7d onto structural windows", () => {
  const result = convertUsageToQuotaInfo({
    quotas: {
      code_5h: { remainingPercentage: 81.2298, resetAt: "2099-09-19T14:24:04.000Z" },
      code_7d: { remainingPercentage: 85.2949, resetAt: "2099-09-25T02:24:04.000Z" },
    },
  });
  assert.ok(result);
  assert.equal(Number(result!.window5h?.percentUsed.toFixed(6)), 0.187702);
  assert.equal(Number(result!.window7d?.percentUsed.toFixed(6)), 0.147051);
  assert.equal(result!.window5h?.resetAt, "2099-09-19T14:24:04.000Z");
  assert.equal(result!.window7d?.resetAt, "2099-09-25T02:24:04.000Z");
});

test("convertUsageToQuotaInfo falls back to used/total when remainingPercentage is absent", () => {
  const result = convertUsageToQuotaInfo({
    quotas: { session: { used: 45, total: 100, resetAt: null } },
  });
  assert.ok(result);
  assert.equal(result!.windows!.session.percentUsed, 0.45);
});

test("convertUsageToQuotaInfo skips unlimited and unmeasurable windows", () => {
  const result = convertUsageToQuotaInfo({
    quotas: {
      session: { remainingPercentage: 50, resetAt: null },
      // No percentage and no used/total → skipped.
      unknown_shape: { resetAt: null },
      // Unlimited windows are intentionally ignored — preflight can't block on them.
      unlimited_credits: { unlimited: true, remainingPercentage: 99 },
    },
  });
  assert.ok(result);
  assert.deepEqual(Object.keys(result!.windows || {}), ["session"]);
});

test("convertUsageToQuotaInfo marks an unlimited-only provider as unlimited, not as a failed read (#15347)", () => {
  // `null` from a fetcher means "could not read it" and is ranked below real readings in
  // auto-combo; a provider with no cap is a real, known reading of full headroom.
  const result = convertUsageToQuotaInfo({
    quotas: { unlimited_thing: { unlimited: true } },
  });
  assert.deepEqual(result, { used: 0, total: 0, percentUsed: 0, unlimited: true });
});

test("convertUsageToQuotaInfo returns null when no window is measurable and none is unlimited", () => {
  const result = convertUsageToQuotaInfo({
    quotas: { unknown_shape: { resetAt: null }, unreported: { fractionReported: false } },
  });
  assert.equal(result, null);
});

test("convertUsageToQuotaInfo clamps remainingPercentage outside 0-100", () => {
  const result = convertUsageToQuotaInfo({
    quotas: {
      a: { remainingPercentage: 150, resetAt: null }, // clamped to 100 → 0% used
      b: { remainingPercentage: -10, resetAt: null }, // clamped to 0 → 100% used
    },
  });
  assert.ok(result);
  assert.equal(result!.windows!.a.percentUsed, 0);
  assert.equal(result!.windows!.b.percentUsed, 1);
});

test("convertUsageToQuotaInfo ignores a window whose fraction was not reported by upstream (#6295)", () => {
  // Antigravity sets fractionReported:false and defaults remainingPercentage
  // to 0 when a model's usage fraction isn't returned upstream. That must
  // NOT be treated as "100% used" — the window should be skipped entirely.
  const result = convertUsageToQuotaInfo({
    quotas: {
      unreported_model: { remainingPercentage: 0, fractionReported: false, resetAt: null },
    },
  });
  assert.equal(result, null);
});

test("convertUsageToQuotaInfo does not let an unreported window inflate worstPercent (#6295)", () => {
  const result = convertUsageToQuotaInfo({
    quotas: {
      reported_low: { remainingPercentage: 80, fractionReported: true, resetAt: null },
      unreported_model: { remainingPercentage: 0, fractionReported: false, resetAt: null },
    },
  });
  assert.ok(result);
  assert.deepEqual(Object.keys(result!.windows || {}), ["reported_low"]);
  assert.equal(result!.percentUsed, 0.2);
  assert.equal(result!.limitReached, false);
});

test("registerGenericQuotaFetchers registers Claude, GLM, and OpenCode Go via the generic adapter", () => {
  registerGenericQuotaFetchers();
  // Claude has no bespoke fetcher → should be registered.
  assert.ok(getQuotaFetcher("claude"), "claude should be registered");
  assert.ok(getQuotaFetcher("glm"), "glm should be registered");
  assert.ok(getQuotaFetcher("zai"), "zai should be registered");
  assert.ok(getQuotaFetcher("opencode-go"), "opencode-go should be registered");
  // Codex has its own dedicated fetcher (registered by codexQuotaFetcher.ts,
  // not by the generic registrar) — the generic registrar skips it. We can't
  // assert "codex" here without first calling registerCodexQuotaFetcher,
  // which would couple this test to chat.ts startup wiring. The skip list
  // semantics are exercised by the source code review.
});

test("convertUsageToQuotaInfo skips Antigravity quota entries with an unknown fraction", () => {
  const result = convertUsageToQuotaInfo({
    quotas: {
      gemini: { fractionReported: false, resetAt: "2026-05-14T20:00:00Z" },
    },
  });
  assert.equal(result, null);
});

test.afterEach(() => {
  __setGenericUsageFetcherForTests(null);
  __resetGenericQuotaFetcherForTests();
  _resetClaudeUsagePollingState();
  _resetClaudeOauthUsageCooldown();
  globalThis.fetch = originalFetch;
  mock.timers.reset();
});

test("fetchGenericQuota caches a hit inside the 60s window", async () => {
  const connectionId = `agy-cache-${Date.now()}`;
  const calls: Array<{ forceRefresh?: boolean }> = [];
  __setGenericUsageFetcherForTests(async (_conn, options) => {
    calls.push({ forceRefresh: options?.forceRefresh });
    return usageShape(80);
  });

  const connection = { provider: "agy", id: connectionId };
  const first = await fetchGenericQuota(connectionId, connection);
  const second = await fetchGenericQuota(connectionId, connection);

  assert.equal(calls.length, 1, "second fetch must reuse the generic cache");
  assert.equal(first?.percentUsed, 0.2);
  assert.deepEqual(second, first);
  invalidateGenericQuotaCache("agy", connectionId);
});

test("invalidateGenericQuotaCache makes the next fetch bypass provider-inner usage caches", async () => {
  const connectionId = `agy-invalidate-${Date.now()}`;
  const calls: Array<{ forceRefresh?: boolean }> = [];
  let remaining = 80;
  __setGenericUsageFetcherForTests(async (_conn, options) => {
    calls.push({ forceRefresh: options?.forceRefresh });
    return usageShape(remaining);
  });

  const connection = { provider: "agy", id: connectionId };
  const first = await fetchGenericQuota(connectionId, connection);
  assert.equal(first?.percentUsed, 0.2);
  assert.equal(calls[0]?.forceRefresh, undefined);

  remaining = 0;
  invalidateGenericQuotaCache("agy", connectionId);
  const second = await fetchGenericQuota(connectionId, connection);

  assert.equal(calls.length, 2, "invalidate must drop the 60s generic cache");
  assert.equal(
    calls[1]?.forceRefresh,
    true,
    "agy retrieveUserQuota / weekly caches are 60s–5min; invalidate must force-refresh or the recache is stale"
  );
  assert.equal(second?.percentUsed, 1);
  invalidateGenericQuotaCache("agy", connectionId);
});

test("invalidateQuotaCacheOnStatus drops cache on 429 and ignores 200", async () => {
  const connectionId = `agy-429-${Date.now()}`;
  const calls: Array<{ forceRefresh?: boolean }> = [];
  __setGenericUsageFetcherForTests(async (_conn, options) => {
    calls.push({ forceRefresh: options?.forceRefresh });
    return usageShape(50);
  });

  const connection = { provider: "agy", id: connectionId };
  await fetchGenericQuota(connectionId, connection);
  assert.equal(calls.length, 1);

  invalidateQuotaCacheOnStatus({
    provider: "agy",
    connectionId,
    status: 200,
    isolateProbe: false,
  });
  await fetchGenericQuota(connectionId, connection);
  assert.equal(calls.length, 1, "200 must not drop the generic quota cache");

  const dropped429 = invalidateQuotaCacheOnStatus({
    provider: "agy",
    connectionId,
    status: 429,
    isolateProbe: false,
  });
  assert.equal(dropped429, true);
  await fetchGenericQuota(connectionId, connection);
  assert.equal(calls.length, 2, "429 must drop the generic quota cache");
  assert.equal(calls[1]?.forceRefresh, true);

  invalidateQuotaCacheOnStatus({
    provider: "agy",
    connectionId,
    status: 429,
    isolateProbe: true,
  });
  await fetchGenericQuota(connectionId, connection);
  assert.equal(calls.length, 2, "probe-origin 429 must not touch routing caches");

  assert.doesNotThrow(() =>
    invalidateQuotaCacheOnStatus({
      provider: "agy",
      connectionId: null,
      status: 429,
      isolateProbe: false,
    })
  );
  invalidateGenericQuotaCache("agy", connectionId);
});

test("invalidateQuotaCacheOnStatus trims to the same key fetchGenericQuota uses", async () => {
  const connectionId = `agy-trim-${Date.now()}`;
  const calls: Array<{ forceRefresh?: boolean }> = [];
  __setGenericUsageFetcherForTests(async (_conn, options) => {
    calls.push({ forceRefresh: options?.forceRefresh });
    return usageShape(40);
  });

  const connection = { provider: "agy", id: connectionId };
  await fetchGenericQuota(`  ${connectionId}  `, connection);
  assert.equal(calls.length, 1);

  const dropped = invalidateQuotaCacheOnStatus({
    provider: "  agy  ",
    connectionId: `  ${connectionId}  `,
    status: 429,
    isolateProbe: false,
  });
  assert.equal(dropped, true);

  await fetchGenericQuota(connectionId, { provider: "agy", id: connectionId });
  assert.equal(calls.length, 2, "padded 429 key must drop the unpadded wrapper cache");
  assert.equal(calls[1]?.forceRefresh, true);
  invalidateGenericQuotaCache("agy", connectionId);
});

test("convert-null after invalidate keeps forceRefresh until a measurable quota recaches", async () => {
  const connectionId = `agy-null-${Date.now()}`;
  const calls: Array<{ forceRefresh?: boolean }> = [];
  let n = 0;
  __setGenericUsageFetcherForTests(async (_conn, options) => {
    calls.push({ forceRefresh: options?.forceRefresh });
    n += 1;
    if (n === 1) return usageShape(80);
    if (n === 2) return { message: "auth expired" };
    return usageShape(10);
  });

  const connection = { provider: "agy", id: connectionId };
  await fetchGenericQuota(connectionId, connection);
  invalidateGenericQuotaCache("agy", connectionId);

  const second = await fetchGenericQuota(connectionId, connection);
  assert.equal(second, null);
  assert.equal(calls[1]?.forceRefresh, true);

  await fetchGenericQuota(connectionId, connection);
  assert.equal(calls.length, 2, "convert-null must not hammer usage inside 60s");

  __agePendingForceRefreshMissForTests("agy", connectionId, 60_000 + 1);
  const fourth = await fetchGenericQuota(connectionId, connection);
  assert.equal(calls.length, 3);
  assert.equal(calls[2]?.forceRefresh, true, "convert-null must not drop the force-refresh flag");
  assert.equal(fourth?.percentUsed, 0.9);
  invalidateGenericQuotaCache("agy", connectionId);
});

test("in-flight fetch must not drop a concurrent 429 force-refresh", async () => {
  const connectionId = `agy-race-${Date.now()}`;
  let release: (value?: unknown) => void = () => {};
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const calls: Array<{ forceRefresh?: boolean }> = [];
  let n = 0;
  __setGenericUsageFetcherForTests(async (_conn, options) => {
    calls.push({ forceRefresh: options?.forceRefresh });
    n += 1;
    if (n === 1) {
      await gate;
      return usageShape(80);
    }
    return usageShape(10);
  });

  const connection = { provider: "agy", id: connectionId };
  const inflight = fetchGenericQuota(connectionId, connection);
  await Promise.resolve();
  invalidateGenericQuotaCache("agy", connectionId);
  release();
  const first = await inflight;
  assert.equal(first?.percentUsed, 0.2);

  const second = await fetchGenericQuota(connectionId, connection);
  assert.equal(
    calls.length,
    2,
    "concurrent 429 must not let the in-flight recache wipe force-refresh"
  );
  assert.equal(calls[1]?.forceRefresh, true);
  assert.equal(second?.percentUsed, 0.9);
  invalidateGenericQuotaCache("agy", connectionId);
});

test("expired pending force-refresh does not bypass the 60s wrapper cache", async () => {
  const connectionId = `agy-expire-${Date.now()}`;
  const calls: Array<{ forceRefresh?: boolean }> = [];
  __setGenericUsageFetcherForTests(async (_conn, options) => {
    calls.push({ forceRefresh: options?.forceRefresh });
    return usageShape(80);
  });

  const connection = { provider: "agy", id: connectionId };
  await fetchGenericQuota(connectionId, connection);
  invalidateGenericQuotaCache("agy", connectionId);
  __agePendingForceRefreshForTests("agy", connectionId, 5 * 60_000 + 1);

  await fetchGenericQuota(connectionId, connection);
  assert.equal(calls.length, 2, "wrapper cache was dropped; fetch still happens");
  assert.equal(
    calls[1]?.forceRefresh,
    undefined,
    "expired force-refresh must not pass forceRefresh after inner caches have aged out"
  );
  invalidateGenericQuotaCache("agy", connectionId);
});

test("stamp expiry during in-flight fetch still writes the wrapper cache", async () => {
  const connectionId = `agy-stamp-expire-${Date.now()}`;
  let release: (value?: unknown) => void = () => {};
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const calls: Array<{ forceRefresh?: boolean }> = [];
  let n = 0;
  __setGenericUsageFetcherForTests(async (_conn, options) => {
    calls.push({ forceRefresh: options?.forceRefresh });
    n += 1;
    if (n === 1) return usageShape(50);
    await gate;
    return usageShape(80);
  });

  const connection = { provider: "agy", id: connectionId };
  await fetchGenericQuota(connectionId, connection);
  invalidateGenericQuotaCache("agy", connectionId);

  const inflight = fetchGenericQuota(connectionId, connection);
  await Promise.resolve();
  __agePendingForceRefreshForTests("agy", connectionId, 5 * 60_000 + 1);
  release();
  await inflight;

  await fetchGenericQuota(connectionId, connection);
  assert.equal(calls.length, 2, "expired stamp during await is not a 429; cache the result");
  invalidateGenericQuotaCache("agy", connectionId);
});

test("convertUsageToQuotaInfo aggregates _freetrial and non-freetrial windows using best remaining", async () => {
  const usage = {
    quotas: {
      credit: { used: 50, total: 50 },
      credit_freetrial: { used: 0, total: 500 },
    },
  };
  const result = convertUsageToQuotaInfo(usage, { provider: "kiro" });
  // Used: min of 100% and 0% = 0%
  assert.equal(result?.percentUsed, 0);
  assert.equal(result?.limitReached, false);
});

test("convertUsageToQuotaInfo blocks when both base and freetrial are exhausted", async () => {
  const usage = {
    quotas: {
      credit: { used: 50, total: 50 },
      credit_freetrial: { used: 500, total: 500 },
    },
  };
  const result = convertUsageToQuotaInfo(usage, { provider: "kiro" });
  assert.equal(result?.percentUsed, 1);
  assert.equal(result?.limitReached, true);
});

test("Claude scoped plan exhaustion and fresh extra usage survive the real quota adapter", async () => {
  const { normalizeClaudeUsageQuotas } = await import("../../open-sse/services/usage/claudeQuota.ts");
  const resetAt = new Date(Date.now() + 3_600_000).toISOString();
  const normalized = normalizeClaudeUsageQuotas({ five_hour: { utilization: 20, resets_at: resetAt },
    seven_day: { utilization: 20, resets_at: resetAt }, seven_day_sonnet: { utilization: 100, resets_at: resetAt } });
  const extraUsage = { is_enabled: true, monthly_limit: 5000, used_credits: 500, currency: "USD", decimal_places: 2 };
  const observedAt = Date.now();
  const usage = { ...normalized, extraUsage, observedAt };
  const opus = convertUsageToQuotaInfo(usage, { provider: "claude", requestedModel: "claude-opus-5-5" });
  const sonnet = convertUsageToQuotaInfo(usage, { provider: "claude", requestedModel: "claude-sonnet-5-5" });
  const scope = { provider: "claude", providerSpecificData: { blockExtraUsage: false } };
  assert.equal(opus?.observedAt, observedAt);
  assert.deepEqual(opus?.extraUsage, extraUsage);
  assert.equal(preflightModule.evaluateQuotaCutoff(opus, undefined, scope).billingRung, "included");
  assert.equal(preflightModule.evaluateQuotaCutoff(sonnet, undefined, scope).billingRung, "credits");
  const stale = convertUsageToQuotaInfo({ ...usage, _stale: true, _staleSince: new Date(observedAt).toISOString() },
    { provider: "claude", requestedModel: "claude-sonnet-5-5" });
  assert.equal(preflightModule.evaluateQuotaCutoff(stale, undefined, scope).proceed, false);
});

const CLAUDE_TEST_NOW = 1_900_000_000_000;
const CLAUDE_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage";
const claudeExtraUsage = {
  is_enabled: true,
  monthly_limit: 5000,
  used_credits: 500,
  currency: "USD",
  decimal_places: 2,
};

function claudeUsageBody(utilization: number, resetAt = "2099-01-01T00:00:00Z") {
  return {
    five_hour: { utilization, resets_at: resetAt },
    seven_day: { utilization: 20, resets_at: "2099-01-07T00:00:00Z" },
    extra_usage: claudeExtraUsage,
  };
}

function installClaudeQuotaFetch(respond: () => Response | Promise<Response>) {
  let usageCalls = 0;
  globalThis.fetch = async (input) => {
    if (String(input) === CLAUDE_OAUTH_USAGE_URL) {
      usageCalls += 1;
      return respond();
    }
    return new Response(null, { status: 404 }); // Best-effort bootstrap is not quota evidence.
  };
  registerGenericQuotaFetchers();
  return () => usageCalls;
}

function claudeConnection(accessToken: string) {
  return {
    provider: "claude",
    accessToken,
    requestedModel: "claude-sonnet-5-5",
    providerSpecificData: { blockExtraUsage: false },
  };
}

test("Claude wrapper refresh preserves t0 at t60 and re-observes usable quota at t66", async () => {
  mock.timers.enable({ apis: ["Date"], now: CLAUDE_TEST_NOW });
  let utilization = 100;
  const calls = installClaudeQuotaFetch(async () => {
    await waitImmediate();
    return Response.json(claudeUsageBody(utilization));
  });
  const connection = claudeConnection("claude-layered-cache");
  const first = await fetchGenericQuota("claude-layered-cache", connection);
  assert.equal(first?.observedAt, CLAUDE_TEST_NOW);
  assert.deepEqual(first?.extraUsage, claudeExtraUsage);
  assert.equal((await preflightModule.preflightQuota("claude", "claude-layered-cache", connection)).billingRung, "credits");

  // TTL expiry is strictly greater than 60s in boundedMap; the inner 65s cache still hits.
  mock.timers.tick(60_001);
  const at60 = await fetchGenericQuota("claude-layered-cache", connection);
  assert.equal(calls(), 1);
  assert.equal(at60?.observedAt, CLAUDE_TEST_NOW, "wrapper refresh cannot re-date inner data");

  utilization = 20;
  mock.timers.tick(5_999);
  const [fresh, decision] = await Promise.all([
    fetchGenericQuota("claude-layered-cache", connection),
    preflightModule.preflightQuota("claude", "claude-layered-cache", connection),
  ]);
  assert.equal(calls(), 2, "concurrent native refreshes must share the existing Claude poll");
  assert.equal(fresh?.observedAt, CLAUDE_TEST_NOW + 66_000);
  assert.deepEqual(fresh?.extraUsage, claudeExtraUsage, "raw extra-usage credit units survive refresh");
  assert.equal(decision.proceed, true);
  assert.equal(decision.billingRung, "included");
});

test("Claude aged credit evidence cannot admit spending during an upstream 429 cooldown", async () => {
  mock.timers.enable({ apis: ["Date"], now: CLAUDE_TEST_NOW });
  let rateLimited = false;
  const calls = installClaudeQuotaFetch(() => rateLimited
    ? new Response(null, { status: 429, headers: { "retry-after": "600" } })
    : Response.json(claudeUsageBody(100)));
  const connection = claudeConnection("claude-layered-cooldown");
  await fetchGenericQuota("claude-layered-cooldown", connection);
  mock.timers.tick(60_001);
  await fetchGenericQuota("claude-layered-cooldown", connection);
  rateLimited = true;
  mock.timers.tick(5_999);
  const stale = await fetchGenericQuota("claude-layered-cooldown", connection);
  assert.equal(stale?.observedAt, CLAUDE_TEST_NOW);
  assert.equal(stale?.stale, true);
  assert.equal(preflightModule.evaluateQuotaCutoff(stale, undefined, connection).reason, "quota_unavailable");
  const denied = await preflightModule.preflightQuota("claude", "claude-layered-cooldown", connection);
  assert.equal(denied.proceed, false);
  assert.equal(denied.reason, "quota_unavailable");
  assert.equal(denied.billingRung, undefined);
  assert.equal(calls(), 2);
  assert.equal(isClaudeOauthUsageCoolingDown(connection.accessToken), true);

  mock.timers.tick(60_001);
  const stillDenied = await preflightModule.preflightQuota("claude", "claude-layered-cooldown", connection);
  assert.equal(stillDenied.proceed, false);
  assert.equal(calls(), 2, "force refresh must preserve the provider's unrelated 429 cooldown");
  assert.equal(isClaudeOauthUsageCoolingDown(connection.accessToken), true);
});

test("Claude unreadable re-observation fails closed and keeps the refresh miss backoff", async () => {
  mock.timers.enable({ apis: ["Date"], now: CLAUDE_TEST_NOW });
  let unreadable = false;
  const calls = installClaudeQuotaFetch(() => unreadable
    ? new Response(null, { status: 503 }) : Response.json(claudeUsageBody(100)));
  const connection = claudeConnection("claude-layered-unreadable");
  await fetchGenericQuota("claude-layered-unreadable", connection);
  mock.timers.tick(60_001);
  await fetchGenericQuota("claude-layered-unreadable", connection);
  unreadable = true;
  mock.timers.tick(5_999);
  const denied = await preflightModule.preflightQuota("claude", "claude-layered-unreadable", connection);
  assert.equal(denied.proceed, false);
  assert.equal(denied.reason, "quota_unavailable");
  assert.equal(denied.billingRung, undefined);
  await preflightModule.preflightQuota("claude", "claude-layered-unreadable", connection);
  assert.equal(calls(), 2, "failed forced reads must not hammer the endpoint");

  unreadable = false;
  mock.timers.tick(60_001);
  const recovered = await preflightModule.preflightQuota("claude", "claude-layered-unreadable", connection);
  assert.equal(calls(), 3);
  assert.equal(recovered.proceed, true);
  assert.equal(recovered.billingRung, "credits", "only the new readable exhaustion can authorize credits");
});

test("Claude elapsed reset re-observes the new period instead of using old exhaustion for credits", async () => {
  mock.timers.enable({ apis: ["Date"], now: CLAUDE_TEST_NOW });
  let utilization = 100;
  let resetAt = new Date(CLAUDE_TEST_NOW + 30_000).toISOString();
  const calls = installClaudeQuotaFetch(() => Response.json(claudeUsageBody(utilization, resetAt)));
  const connection = claudeConnection("claude-layered-reset");
  await fetchGenericQuota("claude-layered-reset", connection);
  utilization = 20;
  resetAt = "2099-01-01T00:00:00Z";
  mock.timers.tick(30_000);
  const decision = await preflightModule.preflightQuota("claude", "claude-layered-reset", connection);
  assert.equal(calls(), 2, "elapsed reset must bypass both otherwise-live cache layers");
  assert.equal(decision.proceed, true);
  assert.equal(decision.billingRung, "included");
  const fresh = await fetchGenericQuota("claude-layered-reset", connection);
  assert.equal(fresh?.observedAt, CLAUDE_TEST_NOW + 30_000);
});

test("Claude elapsed reset with unreadable new-period quota never admits old credit evidence", async () => {
  mock.timers.enable({ apis: ["Date"], now: CLAUDE_TEST_NOW });
  let unreadable = false;
  const resetAt = new Date(CLAUDE_TEST_NOW + 30_000).toISOString();
  const calls = installClaudeQuotaFetch(() => unreadable
    ? new Response(null, { status: 503 }) : Response.json(claudeUsageBody(100, resetAt)));
  const connection = claudeConnection("claude-layered-reset-unreadable");
  await fetchGenericQuota("claude-layered-reset-unreadable", connection);
  unreadable = true;
  mock.timers.tick(30_000);
  const decision = await preflightModule.preflightQuota("claude", "claude-layered-reset-unreadable", connection);
  assert.equal(calls(), 2);
  assert.equal(decision.proceed, false);
  assert.equal(decision.reason, "quota_unavailable");
  assert.equal(decision.billingRung, undefined);
});

test("Claude future-dated cache evidence is re-observed after a clock rollback", async () => {
  mock.timers.enable({ apis: ["Date"], now: CLAUDE_TEST_NOW });
  const calls = installClaudeQuotaFetch(() => Response.json(claudeUsageBody(20)));
  const connection = claudeConnection("claude-layered-clock-rollback");
  await fetchGenericQuota("claude-layered-clock-rollback", connection);
  mock.timers.setTime(CLAUDE_TEST_NOW - 1_000);
  const decision = await preflightModule.preflightQuota("claude", "claude-layered-clock-rollback", connection);
  assert.equal(calls(), 2);
  assert.equal(decision.proceed, true);
  const fresh = await fetchGenericQuota("claude-layered-clock-rollback", connection);
  assert.equal(fresh?.observedAt, CLAUDE_TEST_NOW - 1_000);
});

test("Claude cold model-scope wrapper refreshes an elapsed inner-cache reset", async () => {
  mock.timers.enable({ apis: ["Date"], now: CLAUDE_TEST_NOW });
  let utilization = 100;
  let resetAt = new Date(CLAUDE_TEST_NOW + 30_000).toISOString();
  const calls = installClaudeQuotaFetch(() => Response.json(claudeUsageBody(utilization, resetAt)));
  const connection = claudeConnection("claude-cold-scope-reset");
  await fetchGenericQuota("claude-cold-scope-reset", {
    ...connection,
    requestedModel: "claude-opus-5-5",
  });
  utilization = 20;
  resetAt = "2099-01-01T00:00:00Z";
  mock.timers.tick(30_000);
  const decision = await preflightModule.preflightQuota("claude", "claude-cold-scope-reset", connection);
  assert.equal(calls(), 2, "the cold Sonnet wrapper must not recache expired Opus-period evidence");
  assert.equal(decision.proceed, true);
  assert.equal(decision.billingRung, "included");
});

test("Claude quota conversion preserves original fetchedAt and rejects missing observation times", () => {
  const usage = {
    quotas: { session: { used: 100, total: 100, resetAt: "2099-01-01T00:00:00Z" } },
    extraUsage: claudeExtraUsage,
    fetchedAt: CLAUDE_TEST_NOW,
  };
  const quota = convertUsageToQuotaInfo(usage, { provider: "claude" });
  assert.equal(quota?.observedAt, CLAUDE_TEST_NOW);
  const unknownTime = convertUsageToQuotaInfo({ ...usage, fetchedAt: undefined }, { provider: "claude" });
  assert.equal(Number.isFinite(unknownTime?.observedAt), false);
  assert.equal(preflightModule.evaluateQuotaCutoff(unknownTime, undefined, {
    provider: "claude",
    providerSpecificData: { blockExtraUsage: false },
  }).reason, "quota_unavailable");
});

test("Claude current model windows use authoritative state, not their display fraction, for billing", () => {
  const requestedModel = "claude-fable-5-1";
  for (const state of [
    { active: true, severity: "normal", percent: 100, exhausted: false },
    { active: true, severity: "warning", percent: 100, exhausted: false },
    { active: false, severity: "critical", percent: 100, exhausted: false },
    { active: true, severity: "critical", percent: 37, exhausted: true },
    { active: true, severity: null, percent: 37, exhausted: true },
    { active: true, severity: "critical", percent: undefined, exhausted: true },
  ]) {
    const normalized = normalizeClaudeUsageQuotas({
      five_hour: { utilization: 20, resets_at: "2099-01-01T00:00:00Z" },
      seven_day: { utilization: 20, resets_at: "2099-01-07T00:00:00Z" },
      limits: [{ kind: "weekly_scoped", isActive: state.active, severity: state.severity,
        percent: state.percent, resetsAt: "2099-01-07T00:00:00Z",
        scope: { model: { id: requestedModel } } }],
    });
    for (const allowCredits of [false, true]) {
      const quota = convertUsageToQuotaInfo({ ...normalized, fetchedAt: Date.now(),
        extraUsage: claudeExtraUsage }, { provider: "claude", requestedModel });
      const decision = preflightModule.evaluateQuotaCutoff(quota, undefined, {
        provider: "claude", requestedModel,
        providerSpecificData: { blockExtraUsage: !allowCredits },
      });
      assert.equal(decision.proceed, !state.exhausted || allowCredits, JSON.stringify(state));
      assert.equal(decision.billingRung, !state.exhausted ? "included" : allowCredits ? "credits" : undefined);
      assert.equal(decision.reason, state.exhausted && !allowCredits ? "quota_exhausted" : undefined);
      const factoryDefault = preflightModule.evaluateQuotaCutoff(quota, {
        resolveMinRemainingPercent: () => 2,
        isExplicitCutoff: () => false,
      }, {
        provider: "claude", requestedModel,
        providerSpecificData: { blockExtraUsage: !allowCredits },
      });
      assert.equal(factoryDefault.proceed, !state.exhausted || allowCredits);
      assert.equal(factoryDefault.billingRung, !state.exhausted ? "included" : allowCredits ? "credits" : undefined);
    }
    const otherModel = "claude-sonnet-5-5";
    const unrelated = convertUsageToQuotaInfo({ ...normalized, fetchedAt: Date.now(),
      extraUsage: claudeExtraUsage }, { provider: "claude", requestedModel: otherModel });
    const otherDecision = preflightModule.evaluateQuotaCutoff(unrelated, undefined, {
      provider: "claude", requestedModel: otherModel, providerSpecificData: { blockExtraUsage: false },
    });
    assert.equal(otherDecision.proceed, true);
    assert.equal(otherDecision.billingRung, "included", "another exact model cannot borrow a scoped blocker");
  }
});

test("Claude legacy normalized exhaustion and explicit protective cutoffs remain distinct", () => {
  const scope = { provider: "claude", requestedModel: "claude-fable-5-1",
    providerSpecificData: { blockExtraUsage: false } };
  const legacy = convertUsageToQuotaInfo({
    ...normalizeClaudeUsageQuotas({ five_hour: { utilization: 100, resets_at: "2099-01-01T00:00:00Z" } }),
    fetchedAt: Date.now(), extraUsage: claudeExtraUsage,
  }, scope);
  const legacyDecision = preflightModule.evaluateQuotaCutoff(legacy, undefined, scope);
  assert.equal(legacyDecision.proceed, true);
  assert.equal(legacyDecision.billingRung, "credits");
  const current = convertUsageToQuotaInfo({
    ...normalizeClaudeUsageQuotas({ limits: [{ kind: "session", percent: 100,
      isActive: true, severity: "warning", resetsAt: "2099-01-01T00:00:00Z" }] }),
    fetchedAt: Date.now(), extraUsage: claudeExtraUsage,
  }, scope);
  const noCutoff = preflightModule.evaluateQuotaCutoff(current, { resolveMinRemainingPercent: () => 0 }, scope);
  assert.equal(noCutoff.proceed, true);
  assert.equal(noCutoff.billingRung, "included");
  const protective = preflightModule.evaluateQuotaCutoff(current, { resolveMinRemainingPercent: () => 10 }, scope);
  assert.equal(protective.proceed, false);
  assert.equal(protective.reason, "quota_cutoff");
  assert.equal(protective.billingRung, undefined);
});

test("Claude normalized legacy near-limit quota keeps the no-credit cutoff without inventing exhaustion", () => {
  const quota = convertUsageToQuotaInfo({
    ...normalizeClaudeUsageQuotas({ five_hour: { utilization: 99, resets_at: "2099-01-01T00:00:00Z" } }),
    fetchedAt: Date.now(), extraUsage: claudeExtraUsage,
  }, { provider: "claude" });
  const blocked = preflightModule.evaluateQuotaCutoff(quota, undefined, {
    provider: "claude", providerSpecificData: { blockExtraUsage: true },
  });
  assert.equal(blocked.proceed, false);
  assert.equal(blocked.reason, "quota_cutoff");
  assert.equal(blocked.billingRung, undefined);
  const included = preflightModule.evaluateQuotaCutoff(quota, undefined, {
    provider: "claude", providerSpecificData: { blockExtraUsage: false },
  });
  assert.equal(included.proceed, true);
  assert.equal(included.billingRung, "included", "99% display utilization is not actual exhaustion");
});

test("Claude factory cutoff origin cannot hold display-only current windows but explicit policy can", () => {
  const scope = { provider: "claude", providerSpecificData: { blockExtraUsage: true } };
  const quota = convertUsageToQuotaInfo({
    ...normalizeClaudeUsageQuotas({ limits: [{ kind: "session", percent: 100,
      isActive: true, severity: "normal", resetsAt: "2099-01-01T00:00:00Z" }] }),
    fetchedAt: Date.now(), extraUsage: claudeExtraUsage,
  }, scope);
  const factory = preflightModule.evaluateQuotaCutoff(quota, {
    resolveMinRemainingPercent: () => 2, isExplicitCutoff: () => false,
  }, scope);
  assert.equal(factory.proceed, true);
  assert.equal(factory.billingRung, "included");
  const explicit = preflightModule.evaluateQuotaCutoff(quota, {
    resolveMinRemainingPercent: () => 2, isExplicitCutoff: () => true,
  }, scope);
  assert.equal(explicit.proceed, false);
  assert.equal(explicit.reason, "quota_cutoff");
  assert.equal(explicit.billingRung, undefined);
});
