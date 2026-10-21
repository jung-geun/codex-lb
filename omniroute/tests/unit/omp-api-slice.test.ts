import test from "node:test";
import assert from "node:assert/strict";

import {
  bearerApiKey,
  buildOmpManifest,
  buildOmpQuota,
  connectionAllowed,
} from "../../src/app/api/v1/_helpers/ompApi.ts";

const now = Date.parse("2026-10-21T12:00:00.000Z");
const fresh = new Date(now - 30_000).toISOString();

function exactModelQuota(modelId: string, kind: "session" | "weekly_scoped", remaining: number) {
  return {
    used: 100 - remaining,
    total: 100,
    remainingPercentage: remaining,
    resetAt: new Date(now + 60_000).toISOString(),
    claudeQuota: { kind, active: false, severity: null, scopeKey: `model:${modelId}`, modelId, modelDisplayName: null },
  };
}

test("OMP manifest only exposes connected OAuth models with exact registry limits", () => {
  const manifest = buildOmpManifest(
    [
      { id: "codex-a", provider: "codex", authType: "oauth" },
      { id: "codex-key", provider: "codex", authType: "api_key" },
      { id: "claude-a", provider: "claude", authType: "oauth" },
      { id: "other", provider: "openai", authType: "oauth" },
    ],
    new Map([
      ["codex-a", [
        { id: "gpt-6.1-sol", name: "GPT 6.1 Sol" },
        { id: "not-in-registry", name: "Unverified" },
        { id: "gpt-6-sol", name: "Hidden", visibility: "hide" },
      ]],
      ["codex-key", [{ id: "gpt-6.1-sol", name: "Key connection" }]],
      ["claude-a", [{ id: "claude-opus-5-5", name: "Claude Opus 5.5" }]],
      ["other", [{ id: "gpt-6.1-sol", name: "Other provider" }]],
    ]),
    (provider, model) => provider !== "claude" || model !== "claude-opus-5-5"
  );

  assert.deepEqual(manifest.models.map(({ id }) => id), ["gpt-6.1-sol"]);
});

test("connection restrictions distinguish unrestricted keys from assigned pools", () => {
  assert.equal(connectionAllowed({ id: "all", token: "secret", allowedConnections: [] }, "claude-a"), true);
  assert.equal(connectionAllowed({ id: "scoped", token: "secret", allowedConnections: ["codex-a"] }, "claude-a"), false);
  assert.equal(connectionAllowed({ id: "scoped", token: "secret", allowedConnections: ["codex-a"] }, "codex-a"), true);
});

test("OMP quota stays exact-model scoped and stale or missing windows remain unknown", () => {
  const model = "claude-opus-5-5";
  const result = buildOmpQuota(model, "claude", [
    {
      connectionId: "pool-a",
      observation: {
        fetchedAt: fresh,
        modelQuotas: {
          "session (5h)": exactModelQuota(model, "session", 80),
          "weekly opus (7d)": exactModelQuota(model, "weekly_scoped", 55),
          "weekly other (7d)": exactModelQuota("claude-sonnet-5", "weekly_scoped", 2),
        },
      },
    },
    {
      connectionId: "pool-b",
      observation: {
        fetchedAt: new Date(now - 6 * 60_000).toISOString(),
        modelQuotas: { "session (5h)": exactModelQuota(model, "session", 1) },
      },
    },
    { connectionId: "pool-c", observation: null },
  ], now);

  assert.equal(result.model, model);
  assert.equal(result.provider, "claude");
  assert.equal(result.windows[0].window, "5h");
  assert.equal(result.windows[0].remaining_account_equivalents, 0.8);
  assert.equal(result.windows[0].reporting_pools, 1);
  assert.equal(result.windows[0].unknown_pools, 2);
  assert.equal(result.windows[0].remaining_percent, null);
  assert.equal(result.windows[1].remaining_account_equivalents, 0.55);
  assert.equal(result.windows[1].reporting_pools, 1);
  assert.equal(result.windows[1].unknown_pools, 2);
  assert.equal(result.windows[1].remaining_percent, null);
});

test("OMP quota uses real provider windows without importing another model's observations", () => {
  const model = "gpt-6.1-sol";
  const result = buildOmpQuota(model, "codex", [
    {
      connectionId: "codex-a",
      observation: {
        fetchedAt: fresh,
        quotas: {
          session: { remainingPercentage: 75, windowSeconds: 18_000, resetAt: new Date(now + 60_000).toISOString() },
          weekly: { remainingPercentage: 40, windowSeconds: 604_800, resetAt: new Date(now + 60_000).toISOString() },
        },
        modelQuotas: {
          "gpt-other: session": { remainingPercentage: 90, windowSeconds: 18_000 },
          [model]: { remainingPercentage: 50, windowSeconds: 18_000, resetAt: new Date(now - 1).toISOString() },
        },
      },
    },
    {
      connectionId: "codex-b",
      observation: { fetchedAt: fresh, modelQuotas: { "gpt-other: session": { remainingPercentage: 90, windowSeconds: 18_000 } } },
    },
  ], now);

  assert.equal(result.windows[0].reporting_pools, 1);
  assert.equal(result.windows[0].unknown_pools, 1);
  assert.equal(result.windows[0].remaining_account_equivalents, 0.75);
  assert.equal(result.windows[1].reporting_pools, 1);
  assert.equal(result.windows[1].remaining_account_equivalents, 0.4);
});

test("OMP endpoint accepts only a single bearer credential", () => {
  assert.equal(bearerApiKey(new Request("https://gateway.invalid", { headers: { authorization: "Bearer abc123" } })), "abc123");
  assert.equal(bearerApiKey(new Request("https://gateway.invalid", { headers: { authorization: "Basic abc123" } })), null);
  assert.equal(bearerApiKey(new Request("https://gateway.invalid", { headers: { authorization: "Bearer abc token" } })), null);
});

test("duplicate connections for a verified quota principal count once, distinct seats remain separate", () => {
  const quota = (remainingPercentage: number) => ({
    fetchedAt: fresh,
    quotas: { session: { remainingPercentage, windowSeconds: 18_000 } },
  });
  const result = buildOmpQuota("gpt-6.1-sol", "codex", [
    { connectionId: "same-seat-a", capacityKey: "workspace/user-a", observation: quota(80) },
    { connectionId: "same-seat-b", capacityKey: "workspace/user-a", observation: quota(70) },
    { connectionId: "other-seat", capacityKey: "workspace/user-b", observation: quota(50) },
  ], now);
  assert.equal(result.windows[0].reporting_pools, 2);
  assert.equal(result.windows[0].unknown_pools, 0);
  assert.equal(result.windows[0].remaining_account_equivalents, 1.2);
  assert.equal(result.windows[0].remaining_percent, 60);
});

test("Codex main, Spark and display-only reserve windows are not mixed", () => {
  const pools = [{
    connectionId: "seat",
    observation: {
      fetchedAt: fresh,
      quotas: {
        session: { remainingPercentage: 80, windowSeconds: 18_000 },
        gpt_5_3_codex_spark_session: { remainingPercentage: 10, windowSeconds: 18_000 },
        reserve_session: { remainingPercentage: 1, windowSeconds: 18_000 },
      },
    },
  }];
  assert.equal(buildOmpQuota("gpt-6.1-sol", "codex", pools, now).windows[0].remaining_percent, 80);
  assert.equal(buildOmpQuota("gpt-5.3-codex-spark", "codex", pools, now).windows[0].remaining_percent, 10);
});
