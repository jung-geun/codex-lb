import test from "node:test";
import assert from "node:assert/strict";
import { createChatPipelineHarness } from "./_chatPipelineHarness.ts";

const harness = await createChatPipelineHarness("native-paid-fallback");
// Runtime imports follow harness creation so every DB singleton binds to the isolated DATA_DIR.
const providers = await import("../../src/lib/db/providers.ts");
const models = await import("../../src/lib/db/models.ts");
const settings = await import("../../src/lib/db/settings.ts");
const { invalidateDbCache } = await import("../../src/lib/db/readCache.ts");
const { handleChat } = await import("../../src/sse/handlers/chat.ts");
const { flushProxyLogsSync } = await import("../../src/lib/proxyLogger.ts");
const { closeCallLogArtifactWriter } = await import("../../src/lib/usage/callLogArtifactWriter.ts");
const { withUpstreamStatusCapture } = await import("../../open-sse/utils/upstreamStatusCapture.ts");
const { currentAppliedProxySink } = await import("../../open-sse/utils/proxyFetch.ts");
const model = "claude-opus-5-5";

test.after(async () => {
  await closeCallLogArtifactWriter();
  flushProxyLogsSync();
  await harness.cleanup();
});
test.beforeEach(async () => { flushProxyLogsSync(); await harness.resetStorage(); });

async function setup(options: { subscriptionOnly?: boolean; allowedConnections?: string[] } = {}) {
  const connection = await harness.seedConnection("anthropic");
  await models.replaceSyncedAvailableModelsForConnection("anthropic", connection.id, [{ id: model, name: model }]);
  const key = await harness.seedApiKey({ allowedConnections: options.allowedConnections });
  await settings.updateSettings({
    nativeSubscriptionRouting: { enabled: true, subscriptionOnlyKeyIds: options.subscriptionOnly ? [key.id] : [] },
    requestRetry: 0,
    maxRetryIntervalSec: 0,
  });
  invalidateDbCache();
  return { key, connection };
}

function request(key: string, messages: Array<Record<string, unknown>> = [{ role: "user", content: "Say OK" }], prefix = "claude") {
  return new Request("http://localhost/v1/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
    body: JSON.stringify({ model: `${prefix}/${model}`, max_tokens: 64, messages, stream: false }),
  });
}

function anthropicCompletion() {
  return Response.json({
    id: "msg_fallback", type: "message", role: "assistant", model,
    content: [{ type: "text", text: "OK" }], stop_reason: "end_turn", stop_sequence: null,
    usage: { input_tokens: 7, output_tokens: 2 },
  });
}

function nativeUsage(percent = 20, extraUsage: Record<string, unknown> | null = null) {
  return Response.json({ five_hour: { utilization: percent, resets_at: new Date(Date.now() + 3_600_000).toISOString() },
    seven_day: { utilization: 20, resets_at: new Date(Date.now() + 86_400_000).toISOString() }, extra_usage: extraUsage });
}

test("actual native request reaches exact-model paid connection only after empty subscription pool", async () => {
  const { key, connection } = await setup();
  const attempted: string[] = [];
  globalThis.fetch = async (input) => {
    attempted.push(String(input));
    return anthropicCompletion();
  };
  const response = await handleChat(request(key.key));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-omniroute-selected-connection-id"), connection.id);
  const body = await response.json();
  assert.equal(body.content[0].text, "OK");
  assert.equal(attempted.filter((url) => url.includes("/v1/messages")).length, 1);
});

test("subscription-only key refuses paid fallback and direct API dispatch", async () => {
  const { key } = await setup({ subscriptionOnly: true });
  let upstreamCalls = 0;
  globalThis.fetch = async () => { upstreamCalls++; return anthropicCompletion(); };
  const fallback = await handleChat(request(key.key));
  assert.notEqual(fallback.status, 200);
  const direct = await handleChat(request(key.key, undefined, "anthropic"));
  assert.equal(direct.status, 403);
  assert.equal(upstreamCalls, 0);
});

test("native continuation cannot change subscription ownership to a paid API connection", async () => {
  const { key } = await setup();
  let upstreamCalls = 0;
  globalThis.fetch = async () => { upstreamCalls++; return anthropicCompletion(); };
  const response = await handleChat(request(key.key, [
    { role: "user", content: "Earlier turn" },
    { role: "assistant", content: "Previous subscription answer" },
    { role: "user", content: "Continue" },
  ]));
  assert.notEqual(response.status, 200);
  assert.equal(upstreamCalls, 0);
});

test("transport failure after native dispatch is terminal and never reaches paid fallback", async () => {
  const { key } = await setup();
  await providers.createProviderConnection({
    provider: "claude", authType: "oauth", name: "subscription",
    accessToken: "test-subscription-access", refreshToken: "test-subscription-refresh",
    tokenExpiresAt: new Date(Date.now() + 3_600_000).toISOString(),
    isActive: true, testStatus: "active",
  });
  invalidateDbCache();
  let inferenceCalls = 0;
  globalThis.fetch = async (input) => {
    if (String(input).includes("/v1/messages")) {
      inferenceCalls++;
      throw new TypeError("fetch failed");
    }
    return nativeUsage();
  };
  const response = await handleChat(request(key.key));
  assert.notEqual(response.status, 200);
  assert.equal(inferenceCalls, 1);
});

test("subscription-only keys exclude metered credentials registered under a native provider", async () => {
  const { key } = await setup({ subscriptionOnly: true });
  await providers.createProviderConnection({
    provider: "claude", authType: "apikey", name: "not-a-subscription",
    apiKey: "test-metered-key", isActive: true, testStatus: "active",
  });
  invalidateDbCache();
  let calls = 0;
  globalThis.fetch = async () => { calls++; return anthropicCompletion(); };
  const response = await handleChat(request(key.key));
  assert.notEqual(response.status, 200);
  assert.equal(calls, 0);
});

test("body and header native ownership bindings cannot cross to paid API", async () => {
  const { hasNativeContinuation } = await import("../../src/sse/handlers/chat/nativeSubscriptionRouting.ts");
  for (const field of ["session_id", "conversation_id", "prompt_cache_key"]) {
    assert.equal(hasNativeContinuation({ [field]: "native-session" }), true);
  }
  assert.equal(hasNativeContinuation({ client_metadata: {
    "x-codex-turn-metadata": JSON.stringify({ thread_id: "thread", turn_id: "turn" }),
  } }), true);
  assert.equal(hasNativeContinuation({}, new Headers({ session_id: "11111111-1111-4111-8111-111111111111" })), true);
});

test("accepted native Claude stream is never reopened by stream recovery or watchdog settings", async () => {
  const { key } = await setup();
  await providers.createProviderConnection({
    provider: "claude", authType: "oauth", name: "native-stream",
    accessToken: "test-native-stream-access", refreshToken: "test-native-stream-refresh",
    tokenExpiresAt: new Date(Date.now() + 3_600_000).toISOString(),
    isActive: true, testStatus: "active",
  });
  await settings.updateSettings({ resilienceSettings: { streamRecovery: {
    enabled: true, continueMidStream: true, throughputWatchdog: { enabled: true },
  } } });
  invalidateDbCache();
  let inferenceCalls = 0;
  globalThis.fetch = async (input) => {
    if (!String(input).includes("/v1/messages")) return nativeUsage();
    inferenceCalls++;
    return new Response([
      `event: message_start\ndata: ${JSON.stringify({ type: "message_start", message: {
        id: "msg_partial", type: "message", role: "assistant", model, content: [], usage: { input_tokens: 7, output_tokens: 0 },
      } })}\n\n`,
      `event: content_block_start\ndata: ${JSON.stringify({ type: "content_block_start", index: 0, content_block: { type: "text", text: "" } })}\n\n`,
      `event: content_block_delta\ndata: ${JSON.stringify({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "PARTIAL_NATIVE" } })}\n\n`,
    ].join(""), { headers: { "Content-Type": "text/event-stream" } });
  };
  const req = new Request("http://localhost/v1/messages", {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${key.key}` },
    body: JSON.stringify({ model: `claude/${model}`, max_tokens: 64, stream: true, messages: [{ role: "user", content: "Say OK" }] }),
  });
  const response = await handleChat(req);
  const body = await response.text();
  assert.match(body, /PARTIAL_NATIVE/);
  assert.equal(inferenceCalls, 1);
});

async function nativeAccount(name: string, optIn: boolean, priority: number) {
  return providers.createProviderConnection({ provider: "claude", authType: "oauth", name,
    accessToken: `test-native-${name}`, refreshToken: `test-refresh-${name}`,
    tokenExpiresAt: new Date(Date.now() + 3_600_000).toISOString(), isActive: true, testStatus: "active",
    priority, providerSpecificData: { blockExtraUsage: !optIn } });
}

test("unreadable native usage cannot trigger globally enabled paired API fallback", async () => {
  const { key } = await setup();
  await nativeAccount("unreadable", true, 1);
  invalidateDbCache();
  let quotaCalls = 0;
  let inferenceCalls = 0;
  globalThis.fetch = async (input) => {
    if (String(input).includes("/v1/messages")) {
      inferenceCalls++;
      return anthropicCompletion();
    }
    quotaCalls++;
    return Response.json({ error: "usage temporarily unavailable" }, { status: 503 });
  };
  const response = await handleChat(request(key.key));
  assert.equal(response.status, 429);
  assert.ok((await response.json()).error);
  assert.ok(quotaCalls > 0);
  assert.equal(inferenceCalls, 0);
});

test("unreadable sibling does not block a healthy included native account", async () => {
  const { key } = await setup();
  await nativeAccount("unknown-sibling", true, 1);
  const included = await nativeAccount("healthy-sibling", false, 2);
  invalidateDbCache();
  const inferenceAuth: string[] = [];
  globalThis.fetch = async (input, init) => {
    const authorization = new Headers(init?.headers).get("authorization") ?? "";
    if (!String(input).includes("/v1/messages")) {
      return authorization.includes("unknown-sibling")
        ? Response.json({ error: "usage unavailable" }, { status: 503 }) : nativeUsage();
    }
    inferenceAuth.push(authorization);
    return anthropicCompletion();
  };
  const response = await handleChat(request(key.key));
  assert.equal(response.status, 200);
  assert.equal((await response.json()).content[0].text, "OK");
  assert.equal(response.headers.get("x-omniroute-selected-connection-id"), included.id);
  assert.equal(inferenceAuth.length, 1);
  assert.ok(inferenceAuth[0].includes("test-native-healthy-sibling"));
});

for (const state of ["missing-observation", "stale", "expired-observation", "expired-reset", "mixed-exhausted"] as const) {
  test(`unverified native quota (${state}) refuses both native and paired API inference`, async () => {
    const { key } = await setup();
    const unknown = await nativeAccount(`unknown-${state}`, true, 1);
    const exhausted = state === "mixed-exhausted" ? await nativeAccount("confirmed-exhausted", false, 2) : null;
    invalidateDbCache();
    const quota = await import("../../open-sse/services/quotaPreflight.ts");
    const original = quota.getQuotaFetcher("claude");
    assert.ok(original);
    const observed = new Set<string>();
    quota.registerQuotaFetcher("claude", async (id) => {
      observed.add(id);
      if (id === exhausted?.id) return { observedAt: Date.now(), used: 100, total: 100,
        percentUsed: 1, windows: { session: { percentUsed: 1 } } };
      assert.equal(id, unknown.id);
      if (state === "mixed-exhausted") return null;
      return { observedAt: state === "missing-observation" ? undefined :
        Date.now() - (state === "expired-observation" ? 66_000 : 0),
      stale: state === "stale", used: 100, total: 100, percentUsed: 1,
      windows: { session: { percentUsed: 1,
        resetAt: new Date(Date.now() + (state === "expired-reset" ? -1000 : 3_600_000)).toISOString() } },
      extraUsage: { is_enabled: true, monthly_limit: 1000, used_credits: 200 } };
    });
    let inferenceCalls = 0;
    globalThis.fetch = async () => { inferenceCalls++; return anthropicCompletion(); };
    try {
      const response = await handleChat(request(key.key));
      assert.equal(response.status, 429);
      assert.ok((await response.json()).error);
      assert.ok(observed.has(unknown.id));
      if (exhausted) assert.ok(observed.has(exhausted.id));
      assert.equal(inferenceCalls, 0);
    } finally {
      quota.registerQuotaFetcher("claude", original);
    }
  });
}

test("selected quota read failure stays unverified across consecutive native requests", async () => {
  const { key } = await setup();
  const account = await nativeAccount("selected-read-failure", true, 1);
  invalidateDbCache();
  const quota = await import("../../open-sse/services/quotaPreflight.ts");
  const original = quota.getQuotaFetcher("claude");
  assert.ok(original);
  let quotaReads = 0;
  let usageRecovered = false;
  quota.registerQuotaFetcher("claude", async (id) => {
    assert.equal(id, account.id);
    quotaReads++;
    return usageRecovered || quotaReads === 1 ? { observedAt: Date.now(), used: 20, total: 100,
      percentUsed: 0.2, windows: { session: { percentUsed: 0.2 } } } : null;
  });
  const inferenceAuth: string[] = [];
  globalThis.fetch = async (_input, init) => {
    inferenceAuth.push(new Headers(init?.headers).get("authorization") ?? "");
    return anthropicCompletion();
  };
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      const response = await handleChat(request(key.key));
      assert.equal(response.status, 429);
      assert.ok((await response.json()).error);
      assert.equal(inferenceAuth.length, 0);
      const stored = await providers.getProviderConnectionById(account.id);
      assert.ok(!stored.rateLimitedUntil, "unverified usage must not install a cooldown");
    }
    assert.ok(quotaReads >= 3);
    usageRecovered = true;
    const recovered = await handleChat(request(key.key));
    assert.equal(recovered.status, 200, await recovered.text());
    assert.equal(recovered.headers.get("x-omniroute-selected-connection-id"), account.id);
    assert.deepEqual(inferenceAuth, [`Bearer ${account.accessToken}`]);
  } finally {
    quota.registerQuotaFetcher("claude", original);
  }
});

for (const usageReadable of [true, false]) {
  test(`expired native OAuth refresh precedes mandatory usage (readable=${usageReadable})`, async () => {
    const { key } = await setup();
    const account = await nativeAccount(`expired-${usageReadable}`, false, 1);
    await providers.updateProviderConnection(account.id, {
      tokenExpiresAt: new Date(Date.now() - 60_000).toISOString(),
      expiresAt: new Date(Date.now() - 60_000).toISOString(),
    });
    invalidateDbCache();
    const { OAUTH_ENDPOINTS } = await import("../../open-sse/config/constants.ts");
    const phases: string[] = [];
    let refreshCalls = 0;
    let nativeCalls = 0;
    let apiCalls = 0;
    globalThis.fetch = async (input, init) => {
      const url = String(input);
      if (url === OAUTH_ENDPOINTS.anthropic.token) {
        phases.push("refresh");
        refreshCalls++;
        const body = new URLSearchParams(String(init?.body));
        assert.equal(body.get("grant_type"), "refresh_token");
        assert.equal(body.get("refresh_token"), `test-refresh-expired-${usageReadable}`);
        return Response.json({ access_token: `test-fresh-${usageReadable}`,
          refresh_token: `test-rotated-${usageReadable}`, expires_in: 3600 });
      }
      const authorization = new Headers(init?.headers).get("authorization") ?? "";
      if (url.includes("/v1/messages")) {
        phases.push("inference");
        if (authorization === `Bearer test-fresh-${usageReadable}`) nativeCalls++;
        else apiCalls++;
        return anthropicCompletion();
      }
      phases.push("usage");
      if (authorization !== `Bearer test-fresh-${usageReadable}`) {
        return Response.json({ error: "expired access token" }, { status: 401 });
      }
      return usageReadable ? nativeUsage() : Response.json({ error: "usage unavailable" }, { status: 503 });
    };
    const response = await handleChat(request(key.key));
    const body = await response.text();
    assert.equal(response.status, usageReadable ? 200 : 429, body);
    assert.equal(refreshCalls, 1);
    assert.equal(phases[0], "refresh");
    assert.ok(phases.includes("usage"));
    assert.equal(nativeCalls, usageReadable ? 1 : 0);
    assert.equal(apiCalls, 0);
    if (usageReadable) assert.equal(response.headers.get("x-omniroute-selected-connection-id"), account.id);
    const stored = await providers.getProviderConnectionById(account.id);
    assert.equal(stored.accessToken, `test-fresh-${usageReadable}`);
    assert.equal(stored.refreshToken, `test-rotated-${usageReadable}`);
    assert.equal(stored.providerSpecificData.blockExtraUsage, true);
  });
}

for (const utilization of [90, 100]) {
  test(`explicit paired API last resort remains available after verified native ${utilization === 100 ? "exhaustion" : "reserve cutoff"}`, async () => {
    const { key, connection } = await setup();
    await nativeAccount(`verified-${utilization}`, false, 1);
    await settings.updateSettings({ resilienceSettings: { quotaPreflight: {
      enabled: true, defaultThresholdPercent: 20,
    } } });
    invalidateDbCache();
    let quotaCalls = 0;
    let nativeCalls = 0;
    let apiProbeCalls = 0;
    let inferenceCalls = 0;
    globalThis.fetch = async (input, init) => {
      if (!String(input).includes("/v1/messages")) {
        if (String(input).includes("/api/oauth/usage")) quotaCalls++;
        return nativeUsage(utilization);
      }
      const headers = new Headers(init?.headers);
      const body = JSON.parse(String(init?.body));
      if (headers.get("authorization")?.startsWith("Bearer test-native-")) nativeCalls++;
      else if (body.model !== model) apiProbeCalls++;
      else {
        assert.equal(headers.get("x-api-key"), connection.apiKey);
        inferenceCalls++;
      }
      return anthropicCompletion();
    };
    const response = await handleChat(request(key.key));
    assert.equal(response.status, 200);
    assert.equal((await response.json()).content[0].text, "OK");
    assert.equal(response.headers.get("x-omniroute-selected-connection-id"), connection.id);
    assert.equal(inferenceCalls, 1);
    assert.ok(quotaCalls > 0, "native reserve/exhaustion must be authoritatively read");
    assert.equal(nativeCalls, 0);
    assert.equal(apiProbeCalls, 0, "paid quota probing must not add another inference");
  });
}

for (const status of [401, 403, 429]) {
  test(`confirmed upstream native ${status} retains explicit same-model API fallback`, async () => {
    const { key, connection } = await setup();
    await nativeAccount(`confirmed-${status}`, false, 1);
    invalidateDbCache();
    let nativeCalls = 0;
    let apiCalls = 0;
    // Retain the production transport's status provenance. Replacing global fetch
    // without its wrapper would erase the very rejection evidence under test.
    globalThis.fetch = withUpstreamStatusCapture<Parameters<typeof fetch>>(async (input, init) => {
      if (!String(input).includes("/v1/messages")) return nativeUsage();
      const authorization = new Headers(init?.headers).get("authorization") ?? "";
      if (authorization.includes(`test-native-confirmed-${status}`)) {
        nativeCalls++;
        return Response.json({ type: "error", error: { type: status === 429 ? "rate_limit_error" : "authentication_error",
          message: "Upstream request explicitly rejected" } }, { status });
      }
      assert.equal(new Headers(init?.headers).get("x-api-key"), connection.apiKey);
      assert.equal(authorization, "", "the native OAuth credential must not follow the API fallback");
      assert.equal(JSON.parse(String(init?.body)).model, model);
      apiCalls++;
      return anthropicCompletion();
    }, currentAppliedProxySink);
    const response = await handleChat(request(key.key));
    assert.equal(response.status, 200);
    assert.equal((await response.json()).content[0].text, "OK");
    assert.equal(response.headers.get("x-omniroute-selected-connection-id"), connection.id);
    assert.equal(nativeCalls, 1);
    assert.equal(apiCalls, 1);
  });
}

for (const failure of ["rejected", "transport", "accepted-invalid-json"] as const) {
  test(`paired API ${failure} is terminal without another paid dispatch`, async () => {
    const { key } = await setup();
    await harness.seedConnection("anthropic", { name: "unused-second-paid-key", priority: 2 });
    let inferenceCalls = 0;
    globalThis.fetch = async (input) => {
      assert.ok(String(input).includes("/v1/messages"));
      inferenceCalls++;
      if (failure === "transport") throw new TypeError("connection lost after submission");
      if (failure === "accepted-invalid-json") {
        return new Response('{"id":"accepted-but-truncated"', {
          headers: { "Content-Type": "application/json" },
        });
      }
      return Response.json({ type: "error", error: {
        type: "rate_limit_error", message: "Explicit API rejection",
      } }, { status: 429 });
    };
    const response = await handleChat(request(key.key));
    await response.text();
    assert.notEqual(response.status, 200);
    assert.equal(inferenceCalls, 1);
  });
}

test("paired API budget refusal precedes every paid probe and inference", async () => {
  const { key } = await setup();
  const costRules = await import("../../src/domain/costRules.ts");
  costRules.setBudget(key.id, { dailyLimitUsd: 1, resetInterval: "daily" });
  costRules.recordCost(key.id, 2);
  await settings.updateSettings({ resilienceSettings: { quotaPreflight: {
    enabled: true, defaultThresholdPercent: 20,
  } } });
  invalidateDbCache();
  let upstreamCalls = 0;
  globalThis.fetch = async () => { upstreamCalls++; return anthropicCompletion(); };
  try {
    const response = await handleChat(request(key.key));
    assert.equal(response.status, 429);
    assert.match(await response.text(), /BUDGET_EXCEEDED/);
    assert.equal(upstreamCalls, 0);
  } finally {
    costRules.deleteBudget(key.id);
  }
});

for (const baseWindow of ["session", "weekly"]) {
  for (const source of ["connection", "provider"] as const) {
    test(`Spark native ranking preserves legacy ${source} ${baseWindow}=0 cutoff`, async () => {
      await setup();
      const connection = await providers.createProviderConnection({
        provider: "codex", authType: "oauth", name: `spark-${source}-${baseWindow}`,
        accessToken: "test-spark-access", refreshToken: "test-spark-refresh",
        tokenExpiresAt: new Date(Date.now() + 86_400_000).toISOString(),
        isActive: true, testStatus: "active",
        ...(source === "connection" ? { quotaWindowThresholds: { [baseWindow]: 0 } } : {}),
      });
      if (source === "provider") await settings.updateSettings({ resilienceSettings: { quotaPreflight: {
        providerWindowDefaults: { codex: { [baseWindow]: 0 } },
      } } });
      invalidateDbCache();
      const quota = await import("../../open-sse/services/quotaPreflight.ts");
      const original = quota.getQuotaFetcher("codex");
      assert.ok(original);
      quota.registerQuotaFetcher("codex", async () => ({ observedAt: Date.now(), used: 99, total: 100,
        percentUsed: 0.99, windows: { [`gpt_5_3_codex_spark_${baseWindow}`]: { percentUsed: 0.99 } } }));
      let inferenceCalls = 0;
      globalThis.fetch = async () => { inferenceCalls++; throw new Error("Unexpected network call"); };
      try {
        const auth = await import("../../src/sse/services/auth.ts");
        const selected = await auth.getProviderCredentialsWithQuotaPreflight("codex", null,
          [connection.id], "gpt-5.3-codex-spark");
        assert.equal(selected?.connectionId, connection.id);
        assert.ok(selected && "nativeBillingRung" in selected);
        assert.equal(selected.nativeBillingRung, "included");
        assert.equal(inferenceCalls, 0);
      } finally {
        quota.registerQuotaFetcher("codex", original);
      }
    });
  }
}

test("native continuation credit-budget refusal is terminal through an ordinary combo", async () => {
  const { key } = await setup();
  const credit = await nativeAccount("combo-budget-credit", true, 1);
  const included = await nativeAccount("combo-budget-included", false, 2);
  const combos = await import("../../src/lib/db/combos.ts");
  await combos.createCombo({ name: "native-budget-continuation", strategy: "priority",
    config: { maxRetries: 0, retryDelayMs: 0 }, models: [
      { id: "credit", kind: "model", providerId: "claude", model, connectionId: credit.id },
      { id: "included", kind: "model", providerId: "claude", model, connectionId: included.id },
    ] });
  const costRules = await import("../../src/domain/costRules.ts");
  costRules.setBudget(key.id, { dailyLimitUsd: 1, resetInterval: "daily" });
  costRules.recordCost(key.id, 2);
  invalidateDbCache();
  let inferenceCalls = 0;
  globalThis.fetch = async (input, init) => {
    if (!String(input).includes("/v1/messages")) {
      const auth = new Headers(init?.headers).get("authorization") ?? "";
      return nativeUsage(auth.includes("combo-budget-credit") ? 100 : 20,
        { is_enabled: true, monthly_limit: 1000, used_credits: 200 });
    }
    inferenceCalls++;
    return anthropicCompletion();
  };
  try {
    const response = await handleChat(new Request("http://localhost/v1/messages", {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${key.key}` },
      body: JSON.stringify({ model: "native-budget-continuation", max_tokens: 64, stream: false,
        messages: [{ role: "user", content: "Before" }, { role: "assistant", content: "Native context" },
          { role: "user", content: "Continue" }] }),
    }));
    assert.equal(response.status, 429);
    assert.match(await response.text(), /BUDGET_EXCEEDED/);
    assert.equal(inferenceCalls, 0);
  } finally {
    costRules.deleteBudget(key.id);
  }
});

test("global native routing uses explicit credit fallback before paired API, but prefers healthy included sibling", async () => {
  const { key } = await setup();
  const credit = await nativeAccount("credit-priority", true, 1);
  const included = await nativeAccount("included-sibling", true, 2);
  invalidateDbCache();
  const attempted: string[] = [];
  globalThis.fetch = async (input, init) => {
    const auth = new Headers(init?.headers).get("authorization") ?? "";
    if (!String(input).includes("/v1/messages")) return nativeUsage(auth.includes("included-sibling") ? 20 : 100,
      { is_enabled: true, monthly_limit: 1000, used_credits: 200 });
    attempted.push(auth);
    return anthropicCompletion();
  };
  const planResponse = await handleChat(request(key.key));
  await planResponse.text();
  assert.equal(planResponse.status, 200);
  assert.equal(planResponse.headers.get("x-omniroute-selected-connection-id"), included.id);
  await providers.updateProviderConnection(included.id, { isActive: false });
  invalidateDbCache();
  const creditResponse = await handleChat(request(key.key));
  await creditResponse.text();
  assert.equal(creditResponse.status, 200);
  assert.equal(creditResponse.headers.get("x-omniroute-selected-connection-id"), credit.id);
  assert.equal(attempted.length, 2);
  assert.ok(attempted.every((auth) => auth.includes("test-native-")));
});

test("exhausted key budget refuses native credit rung before dispatch while included subscription remains usable", async () => {
  const { key } = await setup();
  await nativeAccount("budget-credit", true, 1);
  const included = await nativeAccount("budget-included", false, 2);
  const costRules = await import("../../src/domain/costRules.ts");
  costRules.setBudget(key.id, { dailyLimitUsd: 1, resetInterval: "daily" });
  costRules.recordCost(key.id, 2);
  invalidateDbCache();
  let inferenceCalls = 0;
  globalThis.fetch = async (input, init) => {
    const auth = new Headers(init?.headers).get("authorization") ?? "";
    if (!String(input).includes("/v1/messages")) return nativeUsage(auth.includes("budget-included") ? 20 : 100,
      { is_enabled: true, monthly_limit: 1000, used_credits: 200 });
    inferenceCalls++;
    return anthropicCompletion();
  };
  const allowed = await handleChat(request(key.key));
  await allowed.text();
  assert.equal(allowed.status, 200);
  assert.equal(allowed.headers.get("x-omniroute-selected-connection-id"), included.id);
  await providers.updateProviderConnection(included.id, { isActive: false });
  invalidateDbCache();
  const refused = await handleChat(request(key.key));
  assert.equal(refused.status, 429);
  assert.match(await refused.text(), /BUDGET_EXCEEDED/);
  assert.equal(inferenceCalls, 1);
  costRules.deleteBudget(key.id);
});

test("pinned native continuation refuses its exhausted account rather than move to healthy sibling", async () => {
  await setup();
  const credit = await nativeAccount("pinned-credit-off", false, 1);
  const included = await nativeAccount("pinned-included", false, 2);
  invalidateDbCache();
  const quota = await import("../../open-sse/services/quotaPreflight.ts");
  const original = quota.getQuotaFetcher("claude");
  quota.registerQuotaFetcher("claude", async (id) => ({ observedAt: Date.now(), used: 100, total: 100,
    percentUsed: id === credit.id ? 1 : 0.2, windows: { session: { percentUsed: id === credit.id ? 1 : 0.2 } } }));
  try {
    const auth = await import("../../src/sse/services/auth.ts");
    const selected = await auth.getProviderCredentialsWithQuotaPreflight("claude", null,
      [credit.id, included.id], model, { forcedConnectionId: credit.id, nativeContinuation: true });
    assert.ok(selected && "allRateLimited" in selected && selected.allRateLimited);
  } finally {
    if (original) quota.registerQuotaFetcher("claude", original);
  }
});

test("strict subscription-only key never spends credit on an opted-in native account", async () => {
  const { key } = await setup({ subscriptionOnly: true });
  await nativeAccount("strict-credit", true, 1);
  invalidateDbCache();
  let upstreamCalls = 0;
  globalThis.fetch = async () => { upstreamCalls++; return anthropicCompletion(); };
  const response = await handleChat(request(key.key));
  assert.notEqual(response.status, 200);
  assert.equal(upstreamCalls, 0);
});

test("existing provider flags persist independently; only a real local credit-consent transition clears its own policy marker", async () => {
  await setup();
  const connection = await nativeAccount("consent-transition", false, 1);
  const route = await import("../../src/app/api/providers/[id]/route.ts");
  await providers.updateProviderConnection(connection.id, { providerSpecificData: { blockExtraUsage: true, keepSetting: "unchanged" },
    lastErrorSource: "extra_usage", lastErrorType: "quota_exhausted", lastError: "local policy", testStatus: "unavailable",
    rateLimitedUntil: new Date(Date.now() + 3_600_000).toISOString() });
  const update = async (body: Record<string, unknown>) => {
    const result = await route.PUT(new Request(`http://localhost/api/providers/${connection.id}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }), { params: Promise.resolve({ id: connection.id }) });
    assert.equal(result.status, 200);
    return (await result.json()).connection;
  };
  const renamed = await update({ name: "renamed-policy-account" });
  assert.equal(renamed.lastErrorSource, "extra_usage");
  const optedIn = await update({ providerSpecificData: { blockExtraUsage: false } });
  assert.equal(optedIn.providerSpecificData.blockExtraUsage, false);
  assert.equal(optedIn.providerSpecificData.keepSetting, "unchanged");
  assert.equal(optedIn.lastErrorSource, null);
  assert.equal(optedIn.rateLimitedUntil, null);
  assert.equal(optedIn.testStatus, "active");
  for (const field of ["lastError", "lastErrorAt", "lastErrorType", "errorCode"]) {
    assert.equal(optedIn[field], null, `${field} must explicitly confirm the local UI clear`);
  }
  const persisted = await providers.getProviderConnectionById(connection.id);
  assert.equal(persisted.providerSpecificData.blockExtraUsage, false);
  assert.equal(persisted.providerSpecificData.keepSetting, "unchanged");
  assert.ok(!persisted.lastErrorSource && !persisted.rateLimitedUntil);
  await providers.updateProviderConnection(connection.id, { lastErrorSource: "upstream", lastErrorType: "rate_limit", lastError: "upstream cooldown",
    rateLimitedUntil: new Date(Date.now() + 3_600_000).toISOString() });
  const unchanged = await update({ providerSpecificData: { blockExtraUsage: false } });
  assert.equal(unchanged.lastErrorSource, "upstream");
  assert.ok(unchanged.rateLimitedUntil);
  const optedOut = await update({ providerSpecificData: { blockExtraUsage: true } });
  assert.equal(optedOut.providerSpecificData.blockExtraUsage, true);
  assert.equal(optedOut.lastErrorSource, "upstream");
  assert.ok(optedOut.rateLimitedUntil);
  const upstreamTransition = await update({ providerSpecificData: { blockExtraUsage: false } });
  assert.equal(upstreamTransition.providerSpecificData.blockExtraUsage, false);
  assert.equal(upstreamTransition.providerSpecificData.keepSetting, "unchanged");
  assert.equal(upstreamTransition.lastErrorSource, "upstream");
  assert.equal(upstreamTransition.lastError, "upstream cooldown");
  assert.ok(upstreamTransition.rateLimitedUntil);
});
