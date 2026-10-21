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

const core = await import("../../src/lib/db/core.ts");
const keys = await import("../../src/lib/db/apiKeys.ts");
const providers = await import("../../src/lib/db/providers.ts");
const models = await import("../../src/lib/db/models.ts");
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
