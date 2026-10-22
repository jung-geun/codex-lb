import test from "node:test";
import assert from "node:assert/strict";
import { createChatPipelineHarness } from "./_chatPipelineHarness.ts";

const harness = await createChatPipelineHarness("codex-weekly-first");
const providers = await import("../../src/lib/db/providers.ts");
const models = await import("../../src/lib/db/models.ts");
const settings = await import("../../src/lib/db/settings.ts");
const { invalidateDbCache } = await import("../../src/lib/db/readCache.ts");
const { handleChat } = await import("../../src/sse/handlers/chat.ts");
const { invalidateCodexQuotaCache } = await import("../../open-sse/services/codexQuotaFetcher.ts");

const model = "gpt-6.1-sol";
type Usage = { session: number; weekly: number };

test.beforeEach(async () => { await harness.resetStorage(); });
test.after(async () => { await harness.cleanup(); });

function completion() {
  const message = { id: "msg_ok", type: "message", role: "assistant", status: "completed",
    content: [{ type: "output_text", text: "OK", annotations: [] }] };
  const response = { id: "resp_ok", object: "response", status: "completed", model, output: [message],
    output_text: "OK", usage: { input_tokens: 10, output_tokens: 1, total_tokens: 11 } };
  return new Response([
    { type: "response.created", response: { ...response, status: "in_progress", output: [], usage: null } },
    { type: "response.output_text.delta", output_index: 0, content_index: 0, item_id: "msg_ok", delta: "OK" },
    { type: "response.completed", response },
  ].map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(""),
  { headers: { "Content-Type": "text/event-stream" } });
}

function usage({ session, weekly }: Usage) {
  const now = Math.floor(Date.now() / 1000);
  return Response.json({
    rate_limit: { limit_reached: session >= 100 || weekly >= 100,
      primary_window: { used_percent: session, reset_at: now + 3600, limit_window_seconds: 18000 },
      secondary_window: { used_percent: weekly, reset_at: now + 400000, limit_window_seconds: 604800 } },
    credits: { has_credits: true, unlimited: false, overage_limit_reached: false, balance: "5000" },
  });
}

/** Account A (priority 1) opted into credits; account B (priority 2) without credit consent. */
async function pool(a: Usage, b: Usage) {
  const key = await harness.seedApiKey();
  const accounts: Record<string, Usage> = {};
  const ids: Record<string, string> = {};
  for (const [name, state, priority, consent] of [["a", a, 1, true], ["b", b, 2, false]] as const) {
    const account = await providers.createProviderConnection({
      provider: "codex", authType: "oauth", name: `weekly-first-${name}`, priority,
      accessToken: `weekly-first-${name}-${Date.now()}`, refreshToken: `weekly-first-refresh-${name}`,
      tokenExpiresAt: new Date(Date.now() + 3600_000).toISOString(), isActive: true, testStatus: "active",
      providerSpecificData: { allowPaidCredits: consent, workspaceId: `ws-${name}` },
    });
    await models.replaceSyncedAvailableModelsForConnection("codex", account.id, [{ id: model, name: model }]);
    invalidateCodexQuotaCache(account.id);
    accounts[account.accessToken ?? ""] = state;
    ids[name] = account.id;
  }
  const api = await providers.createProviderConnection({ provider: "openai", authType: "apikey",
    name: "paired-api", apiKey: "sk-paired-api-fixture", isActive: true, testStatus: "active" });
  await models.replaceSyncedAvailableModelsForConnection("openai", api.id, [{ id: model, name: model }]);
  await settings.updateSettings({ nativeSubscriptionRouting: { enabled: true }, requestRetry: 0, maxRetryIntervalSec: 0 });
  invalidateDbCache();
  const dispatched: string[] = [];
  globalThis.fetch = async (input, init) => {
    const auth = new Headers(init?.headers).get("authorization") ?? "";
    const token = auth.replace(/^Bearer /, "");
    if (String(input).includes("/wham/usage")) return usage(accounts[token]);
    dispatched.push(token.includes("weekly-first-a") ? "a" : token.includes("weekly-first-b") ? "b" : "api");
    return completion();
  };
  return { key, ids, dispatched };
}

function request(key: string, body: Record<string, unknown> = {}) {
  return handleChat(new Request("http://localhost/v1/responses", {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
    body: JSON.stringify({ model: `codex/${model}`, stream: false, input: "ping", ...body }),
  }));
}

test("credits wait while a sibling only waits on its 5-hour window; neither credits nor paid API are used", async () => {
  const { key, dispatched } = await pool({ session: 100, weekly: 100 }, { session: 100, weekly: 40 });
  const response = await request(key.key);
  await response.text();
  assert.equal(response.status, 429);
  assert.deepEqual(dispatched, []);
});

test("an account blocked only by its own 5-hour window never starts its credits", async () => {
  const { key, dispatched } = await pool({ session: 100, weekly: 30 }, { session: 100, weekly: 100 });
  const response = await request(key.key);
  await response.text();
  assert.equal(response.status, 429);
  assert.deepEqual(dispatched, []);
});

test("a sibling with weekly quota serves before any account's credits", async () => {
  const { key, ids, dispatched } = await pool({ session: 100, weekly: 100 }, { session: 20, weekly: 50 });
  const response = await request(key.key);
  await response.text();
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-omniroute-selected-connection-id"), ids.b);
  assert.deepEqual(dispatched, ["b"]);
});

test("resent conversation history moves to the account with weekly quota instead of using credits", async () => {
  const { key, ids, dispatched } = await pool({ session: 100, weekly: 100 }, { session: 20, weekly: 50 });
  const response = await request(key.key, { input: [
    { role: "user", content: "first" }, { role: "assistant", content: "answer" }, { role: "user", content: "next" },
  ] });
  await response.text();
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-omniroute-selected-connection-id"), ids.b);
  assert.deepEqual(dispatched, ["b"]);
});

const boundInput = { input: [
  { role: "user", content: "first" },
  { type: "reasoning", id: "rs_bound", summary: [], encrypted_content: "opaque-account-bound-reasoning" },
  { role: "user", content: "next" },
] };

test("account-bound state is refused rather than charged while a sibling has weekly quota", async () => {
  const { key, dispatched } = await pool({ session: 100, weekly: 100 }, { session: 20, weekly: 50 });
  const response = await request(key.key, boundInput);
  await response.text();
  assert.notEqual(response.status, 200);
  assert.deepEqual(dispatched, [], "no credit inference on A and no move of A-bound state to B");
});

test("account-bound state uses its own credits once the whole pool's weekly quota is exhausted", async () => {
  const { key, ids, dispatched } = await pool({ session: 100, weekly: 100 }, { session: 100, weekly: 100 });
  const response = await request(key.key, boundInput);
  await response.text();
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-omniroute-selected-connection-id"), ids.a);
  assert.deepEqual(dispatched, ["a"]);
});

test("credits start only after every Codex account's weekly quota is exhausted", async () => {
  const { key, ids, dispatched } = await pool({ session: 100, weekly: 100 }, { session: 100, weekly: 100 });
  const response = await request(key.key);
  await response.text();
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-omniroute-selected-connection-id"), ids.a);
  assert.deepEqual(dispatched, ["a"]);
});
