import test from "node:test";
import assert from "node:assert/strict";
import { createChatPipelineHarness } from "./_chatPipelineHarness.ts";

const harness = await createChatPipelineHarness("native-paid-fallback");
const providers = await import("../../src/lib/db/providers.ts");
const models = await import("../../src/lib/db/models.ts");
const settings = await import("../../src/lib/db/settings.ts");
const { invalidateDbCache } = await import("../../src/lib/db/readCache.ts");
const { handleChat } = await import("../../src/sse/handlers/chat.ts");
const { flushProxyLogsSync } = await import("../../src/lib/proxyLogger.ts");
const { closeCallLogArtifactWriter } = await import("../../src/lib/usage/callLogArtifactWriter.ts");
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
    return Response.json({});
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
    if (!String(input).includes("/v1/messages")) return Response.json({});
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
