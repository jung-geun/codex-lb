import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "omr-omp-auth-scope-"));
process.env.DATA_DIR = dataDir;
process.env.API_KEY_SECRET = "omp-auth-scope-test-secret";
delete process.env.OMNIROUTE_API_KEY;
delete process.env.ROUTER_API_KEY;

// Load DB modules only after setting the isolated DATA_DIR; static imports would open the real profile.
const core = await import("../../src/lib/db/core.ts");
const keys = await import("../../src/lib/db/apiKeys.ts");
const providers = await import("../../src/lib/db/providers.ts");
const models = await import("../../src/lib/db/models.ts");
const settings = await import("../../src/lib/db/settings.ts");
const { GET: manifest } = await import("../../src/app/api/v1/omp/models/route.ts");
const { GET: quota } = await import("../../src/app/api/v1/usage/quota/route.ts");
const { setProviderLimitsCache } = await import("../../src/lib/db/providerLimits.ts");
const { SELF_ACCOUNT_QUOTA_SCOPE, SELF_USAGE_SCOPE } = await import("../../src/shared/constants/selfServiceScopes.ts");

const codex = await providers.createProviderConnection({
  provider: "codex", authType: "oauth", name: "Codex scope fixture", isActive: true,
});
const claude = await providers.createProviderConnection({
  provider: "claude", authType: "oauth", name: "Claude scope fixture", isActive: true,
});
await models.replaceSyncedAvailableModelsForConnection("codex", codex.id, [{ id: "gpt-6.1-sol", name: "GPT 6.1 Sol" }]);
await models.replaceSyncedAvailableModelsForConnection("claude", claude.id, [{ id: "claude-opus-5-5", name: "Claude Opus 5.5" }]);
const antigravity = await providers.createProviderConnection({
  provider: "antigravity", authType: "oauth", name: "Gemini scope fixture", isActive: true,
});
await models.replaceSyncedAvailableModelsForConnection("antigravity", antigravity.id, [
  { id: "gemini-3.8-flash-low", name: "Gemini 3.8 Flash (Low)" },
  { id: "gemini-3.8-flash-tiered", name: "Gemini 3.8 Flash (Tiered)" },
  { id: "gemini-3.1-flash-lite", name: "Hidden Gemini", visibility: "hide" },
  { id: "gemini-3.1-flash-image", name: "Image generation only" },
  { id: "gemini-unverified", name: "No verified limits" },
]);

function request(key?: string) {
  return new Request("https://gateway.invalid/v1/omp/models", {
    headers: key ? { Authorization: `Bearer ${key}` } : {},
  });
}

async function modelIds(key: string): Promise<string[]> {
  const response = await manifest(request(key));
  assert.equal(response.status, 200);
  const body = await response.json();
  return body.models.map((row: { id: string }) => row.id);
}

test.after(() => {
  keys.resetApiKeyState();
  core.resetDbInstance();
  fs.rmSync(dataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
});

test("OMP catalog enforces connection and model scopes before a forced model", async () => {
  const key = await keys.createApiKey("scoped native catalog", "omp-scope-test");
  await keys.updateApiKeyPermissions(key.id, { allowedConnections: [claude.id] });
  assert.deepEqual(await modelIds(key.key), ["claude-opus-5-5"]);

  await keys.updateApiKeyPermissions(key.id, { blockedModels: ["claude/claude-opus-5-5"] });
  assert.deepEqual(await modelIds(key.key), []);

  await keys.updateApiKeyPermissions(key.id, { blockedModels: [], forcedModel: "codex/gpt-6.1-sol" });
  assert.deepEqual(await modelIds(key.key), [], "a pin must not grant an unassigned connection");

  await keys.updateApiKeyPermissions(key.id, { allowedConnections: [] });
  assert.deepEqual(await modelIds(key.key), ["gpt-6.1-sol"]);
});

test("orphaned quota allocation never broadens an otherwise unrestricted native catalog", async () => {
  const key = await keys.createApiKey("orphaned quota", "omp-orphan-test");
  await keys.updateApiKeyPermissions(key.id, { allowedQuotas: ["deleted-pool"] });
  assert.deepEqual(await modelIds(key.key), []);
});

test("OMP catalog does not expose connected model identities without a valid gateway key", async () => {
  assert.equal((await manifest(request())).status, 401);
  assert.equal((await manifest(request("invalid-key"))).status, 401);
});

test("OMP quota requires explicit shared quota permission and respects its revocation", async () => {
  const key = await keys.createApiKey("quota visibility", "omp-quota-scope-test");
  const readQuota = async () => {
    const response = await quota(new Request("https://gateway.invalid/v1/usage/quota?model=gpt-6.1-sol", {
      headers: { Authorization: `Bearer ${key.key}` },
    }));
    assert.equal(response.status, 200);
    return response.json();
  };
  setProviderLimitsCache(codex.id, {
    fetchedAt: new Date().toISOString(), plan: null, message: null,
    quotas: { weekly: { remainingPercentage: 73, windowSeconds: 604_800 } },
  });
  const privateQuota = await readQuota();
  assert.equal(privateQuota.hidden, true);
  assert.equal(privateQuota.provider, null);
  assert.deepEqual(privateQuota.windows, []);

  await keys.updateApiKeyPermissions(key.id, { scopes: [SELF_USAGE_SCOPE, SELF_ACCOUNT_QUOTA_SCOPE] });
  const visibleQuota = await readQuota();
  assert.equal(visibleQuota.hidden, false);
  assert.equal(visibleQuota.provider, "codex");
  assert.equal(visibleQuota.windows.find((row: { window: string }) => row.window === "7d").remaining_percent, 73);

  await keys.updateApiKeyPermissions(key.id, { scopes: [SELF_USAGE_SCOPE] });
  const revokedQuota = await readQuota();
  assert.equal(revokedQuota.hidden, true);
  assert.deepEqual(revokedQuota.windows, []);
});

test("Antigravity Gemini discovery respects exact connection, model and forced-model scopes", async () => {
  const key = await keys.createApiKey("Gemini catalog", "omp-gemini-test");
  await keys.updateApiKeyPermissions(key.id, { allowedConnections: [antigravity.id] });
  assert.deepEqual(await modelIds(key.key), [
    "antigravity/gemini-3.8-flash-low", "antigravity/gemini-3.8-flash-tiered",
  ]);

  await keys.updateApiKeyPermissions(key.id, {
    allowedModels: ["antigravity/*"], blockedModels: ["antigravity/gemini-3.8-flash-tiered"],
  });
  assert.deepEqual(await modelIds(key.key), ["antigravity/gemini-3.8-flash-low"]);

  await keys.updateApiKeyPermissions(key.id, { forcedModel: "antigravity/gemini-3.8-flash-low" });
  assert.deepEqual(await modelIds(key.key), ["antigravity/gemini-3.8-flash-low"]);
  await keys.updateApiKeyPermissions(key.id, { forcedModel: "gemini-3.8-flash-low" });
  assert.deepEqual(await modelIds(key.key), [], "a bare pin cannot preserve Antigravity routing identity");

  await keys.updateApiKeyPermissions(key.id, {
    forcedModel: "antigravity/gemini-3.8-flash-low", allowedConnections: [claude.id],
  });
  assert.deepEqual(await modelIds(key.key), [], "a forced Gemini model cannot grant an unassigned connection");

  await keys.updateApiKeyPermissions(key.id, { allowedConnections: [antigravity.id], catalogScope: "combos" });
  assert.deepEqual(await modelIds(key.key), []);
});

test("Gemini metadata uses the chat protocol and exact limits without borrowing Codex quota", async () => {
  const key = await keys.createApiKey("Gemini metadata", "omp-gemini-metadata-test");
  await keys.updateApiKeyPermissions(key.id, {
    allowedConnections: [antigravity.id], scopes: [SELF_USAGE_SCOPE, SELF_ACCOUNT_QUOTA_SCOPE],
  });
  const response = await manifest(request(key.key));
  const body = await response.json();
  const gemini = body.models.find((row: { id: string }) => row.id === "antigravity/gemini-3.8-flash-low");
  assert.equal(gemini.api, "openai-completions");
  assert.equal(gemini.basePath, "/v1");
  assert.equal(gemini.contextWindow, 1_048_576);
  assert.equal(gemini.maxTokens, 65_536);
  assert.deepEqual(gemini.input, ["text", "image"]);
  assert.equal(gemini.reasoning, true);

  const quotaResponse = await quota(new Request(
    "https://gateway.invalid/v1/usage/quota?model=antigravity%2Fgemini-3.8-flash-low",
    { headers: { Authorization: `Bearer ${key.key}` } }
  ));
  const quotaBody = await quotaResponse.json();
  assert.equal(quotaBody.provider, null);
  assert.deepEqual(quotaBody.windows, []);
});

test("disabled and non-OAuth Antigravity connections cannot expose their synced models", async () => {
  const key = await keys.createApiKey("disabled Gemini", "omp-disabled-gemini-test");
  for (const connection of [
    await providers.createProviderConnection({ provider: "antigravity", authType: "oauth", isActive: false }),
    await providers.createProviderConnection({ provider: "antigravity", authType: "apikey", isActive: true }),
  ]) {
    await models.replaceSyncedAvailableModelsForConnection("antigravity", connection.id, [
      { id: "gemini-4-argon-high", name: "Unusable Gemini connection" },
    ]);
    await keys.updateApiKeyPermissions(key.id, { allowedConnections: [connection.id] });
    assert.deepEqual(await modelIds(key.key), []);
  }
});

test("Gemini discovery evaluates the same qualified model permission as inference", async () => {
  const key = await keys.createApiKey("qualified Gemini permission", "omp-gemini-permission-test");
  const qualified = "antigravity/gemini-3.8-flash-low";
  await keys.updateApiKeyPermissions(key.id, {
    allowedConnections: [antigravity.id], allowedModels: ["gemini-3.8-flash-low"],
  });
  assert.equal(await keys.isModelAllowedForKey(key.key, qualified), false);
  assert.deepEqual(await modelIds(key.key), []);

  await keys.updateApiKeyPermissions(key.id, { allowedModels: [qualified] });
  assert.equal(await keys.isModelAllowedForKey(key.key, qualified), true);
  assert.deepEqual(await modelIds(key.key), [qualified]);

  await keys.updateApiKeyPermissions(key.id, { disableNonPublicModels: true });
  assert.equal(await keys.isModelAllowedForKey(key.key, qualified), true);
  assert.deepEqual(await modelIds(key.key), [qualified]);
});

test("native Codex/Claude-only billing keys cannot discover Gemini routes they cannot call", async () => {
  const key = await keys.createApiKey("native-only catalog", "omp-native-only-catalog-test");
  await keys.updateApiKeyPermissions(key.id, { allowedConnections: [codex.id, antigravity.id] });
  await settings.updateSettings({
    nativeSubscriptionRouting: { enabled: true, subscriptionOnlyKeyIds: [key.id] },
  });
  try {
    assert.deepEqual(await modelIds(key.key), ["gpt-6.1-sol"]);
  } finally {
    await settings.updateSettings({ nativeSubscriptionRouting: { enabled: false, subscriptionOnlyKeyIds: [] } });
  }
  assert.deepEqual(await modelIds(key.key), [
    "gpt-6.1-sol", "antigravity/gemini-3.8-flash-low", "antigravity/gemini-3.8-flash-tiered",
  ]);
});

test("Antigravity discovers Gemini 4, Opus, Sonnet and GPT-OSS without changing their route owner", async () => {
  const connection = await providers.createProviderConnection({
    provider: "antigravity", authType: "oauth", name: "Antigravity model families", isActive: true,
  });
  const ids = [
    "gemini-4-argon-high", "claude-opus-5-5-medium", "claude-sonnet-5-5-high", "gpt-oss-120b-medium",
  ];
  await models.replaceSyncedAvailableModelsForConnection("antigravity", connection.id, [
    ...ids.map((id) => ({ id, name: id })),
    { id: "claude-opus-5-5-low", name: "Hidden Opus", visibility: "hide" },
    { id: "gemini-3.1-flash-image", name: "Image generation" },
    { id: "unverified-chat", name: "No verified limits" },
  ]);
  const key = await keys.createApiKey("Antigravity model families", "omp-antigravity-families-test");
  await keys.updateApiKeyPermissions(key.id, {
    allowedConnections: [connection.id], scopes: [SELF_USAGE_SCOPE, SELF_ACCOUNT_QUOTA_SCOPE],
  });
  const response = await manifest(request(key.key));
  const body = await response.json();
  assert.deepEqual(body.models.map((row: { id: string }) => row.id), ids.map((id) => `antigravity/${id}`));
  for (const row of body.models) {
    assert.equal(row.api, "openai-completions");
    assert.equal(row.basePath, "/v1");
    assert.equal(row.contextWindow, row.id.includes("gpt-oss") ? 131_072 : 1_048_576);
  }
  const opus = "antigravity/claude-opus-5-5-medium";
  await keys.updateApiKeyPermissions(key.id, { allowedModels: ["claude/*"] });
  assert.deepEqual(await modelIds(key.key), [], "native Claude permissions must not grant Antigravity models");
  await keys.updateApiKeyPermissions(key.id, { allowedModels: [opus], forcedModel: opus });
  assert.deepEqual(await modelIds(key.key), [opus]);
  await keys.updateApiKeyPermissions(key.id, { blockedModels: [opus] });
  assert.deepEqual(await modelIds(key.key), [], "a forced Antigravity Opus pin must not override a deny");
  const quotaResponse = await quota(new Request(
    `https://gateway.invalid/v1/usage/quota?model=${encodeURIComponent(opus)}`,
    { headers: { Authorization: `Bearer ${key.key}` } }
  ));
  const quotaBody = await quotaResponse.json();
  assert.equal(quotaBody.provider, null, "Antigravity Opus must not borrow the native Claude quota");
  assert.deepEqual(quotaBody.windows, []);
});

test("native Claude discovers account-synced Haiku 5.5 with canonical limits and unchanged key restrictions", async () => {
  const connection = await providers.createProviderConnection({
    provider: "claude", authType: "oauth", name: "Haiku discovery", isActive: true,
  });
  await models.replaceSyncedAvailableModelsForConnection("claude", connection.id, [
    { id: "claude-haiku-5-5", name: "Claude Haiku 5.5" },
    { id: "claude-haiku-4-5-20251001", name: "Claude Haiku 4.5" },
    { id: "claude-unverified", name: "No verified limits" },
    { id: "claude-sonnet-5-5", name: "Hidden Sonnet", visibility: "hide" },
  ]);
  const key = await keys.createApiKey("Haiku discovery", "omp-haiku-discovery-test");
  await keys.updateApiKeyPermissions(key.id, { allowedConnections: [connection.id] });
  const response = await manifest(request(key.key));
  const body = await response.json();
  assert.deepEqual(body.models.map((row: { id: string }) => row.id), [
    "claude-haiku-5-5", "claude-haiku-4-5-20251001",
  ]);
  const haiku = body.models[0];
  assert.equal(haiku.api, "anthropic-messages");
  assert.equal(haiku.basePath, "/");
  assert.equal(haiku.contextWindow, 1_000_000);
  assert.equal(haiku.maxTokens, 128_000);
  assert.equal(haiku.reasoning, true);
  assert.deepEqual(haiku.input, ["text", "image"]);
  assert.equal(body.models[1].contextWindow, 200_000);
  assert.equal(body.models[1].maxTokens, 64_000);

  await keys.updateApiKeyPermissions(key.id, {
    allowedModels: ["claude/claude-haiku-5-5"], forcedModel: "claude-haiku-5-5",
  });
  assert.deepEqual(await modelIds(key.key), ["claude-haiku-5-5"]);
  await keys.updateApiKeyPermissions(key.id, { blockedModels: ["claude/claude-haiku-5-5"] });
  assert.deepEqual(await modelIds(key.key), [], "a forced Haiku pin cannot override a deny");
  await keys.updateApiKeyPermissions(key.id, { blockedModels: [], allowedConnections: [codex.id] });
  assert.deepEqual(await modelIds(key.key), [], "a Haiku pin cannot grant an unassigned Claude account");
});
