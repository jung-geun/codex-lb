import test from "node:test";
import assert from "node:assert/strict";

import {
  fetchCodexQuota,
  getCodexQuotaCooldownMs,
  invalidateCodexQuotaCache,
  registerCodexConnection,
  registerCodexQuotaFetcher,
} from "../../open-sse/services/codexQuotaFetcher.ts";
import { preflightQuota } from "../../open-sse/services/quotaPreflight.ts";
import {
  clearQuotaMonitors,
  getActiveMonitorCount,
  startQuotaMonitor,
  stopQuotaMonitor,
} from "../../open-sse/services/quotaMonitor.ts";
import { clearSessions, touchSession } from "../../open-sse/services/sessionManager.ts";

const originalFetch = globalThis.fetch;

test.afterEach(() => {
  globalThis.fetch = originalFetch;
  clearQuotaMonitors();
  clearSessions();
});

test("fetchCodexQuota returns null when no registered credentials exist", async () => {
  const quota = await fetchCodexQuota(`missing-${Date.now()}`);
  assert.equal(quota, null);
});

test("fetchCodexQuota exposes available paid credits without changing exhausted subscription windows", async () => {
  globalThis.fetch = async () =>
    Response.json({
      rate_limit: { limit_reached: true, primary_window: { used_percent: 100 } },
      credits: { has_credits: true, unlimited: false, overage_limit_reached: false, balance: null },
    });
  const connectionId = "codex-paid-credits-fetch";
  try {
    const quota = await fetchCodexQuota(connectionId, {
      accessToken: "test-token",
      providerSpecificData: { workspaceId: "test-workspace" },
    });
    assert.equal(quota?.paidCredits?.hasCredits, true);
    assert.equal(quota?.paidCredits?.balance, null);
    assert.equal(quota?.limitReached, true);
    assert.equal(quota?.windows?.session.percentUsed, 1);
  } finally {
    invalidateCodexQuotaCache(connectionId);
  }
});

test("fetchCodexQuota can read credentials directly from the provided connection snapshot", async () => {
  const connectionId = `codex-inline-${Date.now()}`;
  const calls = [];

  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(
      JSON.stringify({
        rate_limit: {
          primary_window: {
            used_percent: 70,
            reset_after_seconds: 45,
          },
          secondary_window: {
            used_percent: 20,
            reset_after_seconds: 300,
          },
        },
      }),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      }
    );
  };

  const quota = await fetchCodexQuota(connectionId, {
    accessToken: "inline-token",
    providerSpecificData: {
      workspaceId: "workspace-inline",
    },
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.headers.Authorization, "Bearer inline-token");
  assert.equal(calls[0].init.headers["chatgpt-account-id"], "workspace-inline");
  assert.equal(quota.percentUsed, 0.7);
  assert.ok(typeof quota.resetAt === "string");

  invalidateCodexQuotaCache(connectionId);
});

test("fetchCodexQuota parses dual-window usage, forwards workspace headers, and caches results", async () => {
  const connectionId = `codex-cache-${Date.now()}`;
  const calls = [];

  registerCodexConnection(connectionId, {
    accessToken: "access-token",
    workspaceId: "workspace-123",
  });

  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(
      JSON.stringify({
        rate_limit: {
          primary_window: {
            used_percent: 80,
            reset_after_seconds: 60,
          },
          secondary_window: {
            used_percent: 40,
            reset_at: Math.floor((Date.now() + 3600_000) / 1000),
          },
        },
      }),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      }
    );
  };

  const first = await fetchCodexQuota(connectionId);
  const second = await fetchCodexQuota(connectionId);

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://chatgpt.com/backend-api/wham/usage");
  assert.equal(calls[0].init.headers.Authorization, "Bearer access-token");
  assert.equal(calls[0].init.headers["chatgpt-account-id"], "workspace-123");
  assert.equal(first.percentUsed, 0.8);
  assert.equal(first.window5h.percentUsed, 0.8);
  assert.equal(first.window7d.percentUsed, 0.4);
  assert.deepEqual(second, first);

  invalidateCodexQuotaCache(connectionId);
});

test("fetchCodexQuota evaluates normal and Spark windows independently by requested model", async () => {
  const connectionId = `codex-spark-scope-${Date.now()}`;
  let calls = 0;

  registerCodexConnection(connectionId, {
    accessToken: "access-token-spark",
  });

  globalThis.fetch = async () => {
    calls++;
    return new Response(
      JSON.stringify({
        rate_limit: {
          primary_window: { used_percent: 20, reset_after_seconds: 60 },
          secondary_window: { used_percent: 30, reset_after_seconds: 120 },
        },
        additional_rate_limits: [
          {
            limit_id: "codex_bengalfox",
            limit_name: "GPT-5.3-Codex-Spark",
            metered_feature: "gpt_5_3_codex_spark",
            rate_limit: {
              primary_window: { used_percent: 100, reset_after_seconds: 300 },
              secondary_window: { used_percent: 40, reset_after_seconds: 600 },
            },
          },
        ],
      }),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      }
    );
  };

  const normal = await fetchCodexQuota(connectionId, { requestedModel: "gpt-5.3-codex" });
  const spark = await fetchCodexQuota(connectionId, { requestedModel: "gpt-5.3-codex-spark" });

  assert.equal(calls, 2, "normal and Spark scopes use separate cache entries");
  assert.equal(normal.percentUsed, 0.3);
  assert.equal(normal.windows?.session.percentUsed, 0.2);
  assert.equal(normal.windows?.weekly.percentUsed, 0.3);
  assert.equal(normal.windows?.gpt_5_3_codex_spark_session, undefined);
  assert.equal(spark.percentUsed, 1);
  assert.equal(spark.windows?.gpt_5_3_codex_spark_session.percentUsed, 1);
  assert.equal(spark.windows?.gpt_5_3_codex_spark_weekly.percentUsed, 0.4);
  assert.equal(spark.windows?.session, undefined);

  const sparkCached = await fetchCodexQuota(connectionId, {
    requestedModel: "gpt-5.3-codex-spark",
  });
  assert.equal(calls, 2);
  assert.deepEqual(sparkCached, spark);

  invalidateCodexQuotaCache(connectionId);
});

test("fetchCodexQuota drops bad credentials after an authorization failure", async () => {
  const connectionId = `codex-auth-${Date.now()}`;
  let calls = 0;

  registerCodexConnection(connectionId, {
    accessToken: "expired-token",
  });

  globalThis.fetch = async () => {
    calls++;
    return new Response("unauthorized", { status: 401 });
  };

  const first = await fetchCodexQuota(connectionId);
  const second = await fetchCodexQuota(connectionId);

  assert.equal(first, null);
  assert.equal(second, null);
  assert.equal(calls, 1);
});

test("getCodexQuotaCooldownMs prefers the 7d window before the 5h window", () => {
  const now = Date.now();
  const quota = {
    used: 99,
    total: 100,
    percentUsed: 0.99,
    window5h: {
      percentUsed: 0.99,
      resetAt: new Date(now + 60_000).toISOString(),
    },
    window7d: {
      percentUsed: 0.97,
      resetAt: new Date(now + 300_000).toISOString(),
    },
    limitReached: false,
  };

  const cooldownMs = getCodexQuotaCooldownMs(quota);

  assert.ok(cooldownMs >= 295_000);
  assert.ok(cooldownMs <= 300_000);
});

test("registerCodexQuotaFetcher exposes Codex quota to preflight and monitor flows", async () => {
  const connectionId = `codex-preflight-${Date.now()}`;

  registerCodexQuotaFetcher();
  registerCodexConnection(connectionId, {
    accessToken: "quota-token",
  });

  // Use 100% (fully exhausted) to avoid floating-point boundary issues:
  // (1 - 0.98) * 100 = 2.0000000000000018, which is > DEFAULT_MIN_REMAINING_PERCENT (2),
  // so the preflight wouldn't block. 100% used → 0% remaining, clearly below 2%.
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        rate_limit: {
          primary_window: { used_percent: 100, reset_after_seconds: 90 },
        },
      }),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      }
    );

  const preflight = await preflightQuota("codex", connectionId, {
    providerSpecificData: { quotaPreflightEnabled: true },
  });

  touchSession("session-codex", connectionId);
  startQuotaMonitor("session-codex", "codex", connectionId, {
    providerSpecificData: { quotaMonitorEnabled: true },
  });

  assert.equal(preflight.proceed, false);
  assert.equal(preflight.reason, "quota_exhausted");
  assert.equal(getActiveMonitorCount(), 1);

  stopQuotaMonitor("session-codex");
  assert.equal(getActiveMonitorCount(), 0);
});

test("evidenced exact-model reserve remains included and never leaks into another model or Spark", async () => {
  const connectionId = "codex-reserve-billing";
  registerCodexQuotaFetcher();
  const connection = { accessToken: "reserve-fixture", providerSpecificData: { allowPaidCredits: false } };
  globalThis.fetch = async () => Response.json({
    rate_limit: { limit_reached: true, primary_window: { used_percent: 100, reset_after_seconds: 3600 } },
    rate_limits_by_limit_id: { "gpt-reserve": { normal_model_slug: "gpt-5.6-luna", rate_limit: {
      primary_window: { used_percent: 20, reset_after_seconds: 3600 }, limit_reached: false,
    } } },
    additional_rate_limits: [{ limit_name: "codex-spark", rate_limit: {
      primary_window: { used_percent: 100, reset_after_seconds: 3600 }, limit_reached: true,
    } }],
    credits: { has_credits: true, balance: 1000 },
  });
  try {
    assert.equal((await preflightQuota("codex", connectionId, { ...connection, requestedModel: "gpt-5.6-luna" })).billingRung, "included");
    const decorated = await preflightQuota("codex", connectionId, {
      ...connection, requestedModel: "codex/gpt-5.6-luna(max)",
    });
    assert.equal(decorated.proceed, true);
    assert.equal(decorated.billingRung, "included", "effort selection retains the actual model's reserve");
    const realSku = await preflightQuota("codex", connectionId, {
      ...connection, requestedModel: "gpt-5.6-luna(max)",
      modelMetadata: { model: "gpt-5.6-luna(max)", supportedThinkingEfforts: ["max"] },
    });
    assert.equal(realSku.proceed, false);
    assert.equal(realSku.reason, "quota_exhausted", "a genuine catalog SKU cannot borrow its name's base reserve");
    assert.equal(realSku.billingRung, undefined);
    assert.equal((await preflightQuota("codex", connectionId, { ...connection, requestedModel: "gpt-6.1-sol" })).proceed, false);
    assert.equal((await preflightQuota("codex", connectionId, { ...connection, providerSpecificData: { allowPaidCredits: true }, requestedModel: "gpt-5.3-codex-spark" })).proceed, false);
  } finally { invalidateCodexQuotaCache(connectionId); }
});

test("real Codex fetcher never converts missing or malformed utilization into included admission", async () => {
  registerCodexQuotaFetcher();
  for (const [index, usedPercent] of [undefined, null, "", "20invalid", "NaN", false, -1].entries()) {
    const connectionId = `codex-unknown-utilization-${index}`;
    globalThis.fetch = async () => Response.json({ rate_limit: {
      primary_window: { ...(usedPercent === undefined ? {} : { used_percent: usedPercent }), reset_after_seconds: 3600 },
    } });
    const connection = { accessToken: "unknown-utilization-fixture", requestedModel: "gpt-6.1-sol",
      providerSpecificData: { allowPaidCredits: false } };
    try {
      const quota = await fetchCodexQuota(connectionId, connection);
      assert.ok(quota);
      assert.equal(Number.isNaN(quota.windows?.session.percentUsed), true);
      const decision = await preflightQuota("codex", connectionId, connection);
      assert.equal(decision.proceed, false);
      assert.equal(decision.reason, "quota_unavailable");
    } finally { invalidateCodexQuotaCache(connectionId); }
  }
});

test("unknown applicable reserve utilization cannot authorize either included or credit admission", async () => {
  registerCodexQuotaFetcher();
  const connectionId = "codex-unknown-reserve-utilization";
  const connection = { accessToken: "unknown-reserve-fixture", requestedModel: "gpt-5.6-luna",
    providerSpecificData: { allowPaidCredits: true } };
  globalThis.fetch = async () => Response.json({
    rate_limit: { limit_reached: true, primary_window: { used_percent: 100, reset_after_seconds: 3600 } },
    rate_limits_by_limit_id: { "gpt-reserve": { normal_model_slug: "gpt-5.6-luna", rate_limit: {
      primary_window: { reset_after_seconds: 3600 },
    } } },
    credits: { has_credits: true, balance: 1000 },
  });
  try {
    const quota = await fetchCodexQuota(connectionId, connection);
    assert.equal(Number.isNaN(quota?.reserveWindows?.session.percentUsed), true);
    const decision = await preflightQuota("codex", connectionId, connection);
    assert.equal(decision.proceed, false);
    assert.equal(decision.reason, "quota_unavailable");
  } finally { invalidateCodexQuotaCache(connectionId); }
});

test("both Codex reserve exhaustion flag spellings require paid consent despite lagging headroom", async () => {
  registerCodexQuotaFetcher();
  for (const flag of ["limit_reached", "limitReached"]) {
    const connectionId = `codex-reserve-exhaustion-${flag}`;
    globalThis.fetch = async () => Response.json({
      rate_limit: { limit_reached: true, primary_window: { used_percent: 100, reset_after_seconds: 3600 } },
      rate_limits_by_limit_id: { "gpt-reserve": {
        normal_model_slug: "gpt-5.6-luna", rate_limit: {
          [flag]: true, primary_window: { used_percent: 20, reset_after_seconds: 3600 },
        },
      } },
      credits: { has_credits: true, balance: 1000 },
    });
    try {
      for (const allowPaidCredits of [false, true]) {
        const decision = await preflightQuota("codex", connectionId, {
          accessToken: "reserve-exhaustion-fixture", requestedModel: "gpt-5.6-luna(max)",
          providerSpecificData: { allowPaidCredits },
        });
        assert.equal(decision.proceed, allowPaidCredits, flag);
        assert.equal(decision.billingRung, allowPaidCredits ? "credits" : undefined, flag);
        assert.equal(decision.reason, allowPaidCredits ? undefined : "quota_exhausted", flag);
      }
    } finally { invalidateCodexQuotaCache(connectionId); }
  }
});
