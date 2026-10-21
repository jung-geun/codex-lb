import test from "node:test";
import assert from "node:assert/strict";
import { createProviderConnection } from "../../src/lib/db/providers.ts";
import { getDbInstance, resetDbInstance } from "../../src/lib/db/core.ts";
import { quotaCapacityIdentity } from "../../src/lib/quota/capacityIdentity.ts";
import { setProviderLimitsCache } from "../../src/lib/db/providerLimits.ts";
import { getConnectionQuotaCapacity } from "../../src/lib/services/quotaCapacity.ts";

test("reused connection cannot estimate across an intervening identity or revive older live cache", async () => {
  const now = Date.now();
  const principal = { accountUUID: "seat-a", organizationUUID: "org" };
  const connection = await createProviderConnection({ provider: "claude", authType: "oauth", name: "continuity", providerSpecificData: principal });
  const id = String(connection.id);
  const db = getDbInstance();
  const identity = quotaCapacityIdentity("claude", principal);
  const other = quotaCapacityIdentity("claude", { ...principal, accountUUID: "seat-b" });
  const snapshot = db.prepare(`INSERT INTO quota_snapshots(provider,connection_id,window_key,remaining_percentage,is_exhausted,next_reset_at,window_duration_ms,raw_data,created_at) VALUES('claude',?,'session',?,0,?,18000000,?,?)`);
  const reset = new Date(now + 3_600_000).toISOString();
  snapshot.run(id, 90, reset, JSON.stringify({ capacityIdentity: identity }), new Date(now - 600_000).toISOString());
  snapshot.run(id, 85, reset, JSON.stringify({ capacityIdentity: other }), new Date(now - 300_000).toISOString());
  snapshot.run(id, 80, reset, JSON.stringify({ capacityIdentity: identity }), new Date(now - 60_000).toISOString());
  db.prepare(`INSERT INTO call_logs(id,provider,connection_id,model,status,timestamp,duration,tokens_in,tokens_out,usage_provenance) VALUES('continuity-call','claude',?,'claude-opus-5-5',200,?,1000,800,200,'reported')`).run(id, new Date(now - 500_000).toISOString());
  setProviderLimitsCache(id, { quotas: { session: { remainingPercentage: 85, resetAt: reset } }, plan: "team", message: null, fetchedAt: new Date(now - 400_000).toISOString() });
  const result = getConnectionQuotaCapacity(id, now)!;
  assert.notEqual(result.windows[0].status, "estimated");
  assert.equal(result.windows[0].sampleCount, 0);
  assert.equal(result.windows[0].estimatedTotalTokens, null);
  resetDbInstance();
});
