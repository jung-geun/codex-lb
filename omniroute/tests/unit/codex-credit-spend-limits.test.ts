import test from "node:test";
import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";

process.env.API_KEY_SECRET = "credit-visuals-isolated-test-secret";
const spend = await import("../../src/lib/providers/codexCreditSpend.ts");
const { evaluateQuotaCutoff, preflightQuota } = await import("../../open-sse/services/quotaPreflight.ts");
const { invalidateCodexQuotaCache, registerCodexQuotaFetcher } =
  await import("../../open-sse/services/codexQuotaFetcher.ts");
const { normalizeProviderSpecificData } = await import("../../src/lib/providers/requestDefaults.ts");

const HOUR = 60 * 60 * 1000;
const originalFetch = globalThis.fetch;
test.afterEach(() => { globalThis.fetch = originalFetch; });

test("consumption sums balance decreases; top-ups and out-of-order observations are not consumption", () => {
  const id = `spend-basic-${Date.now()}`;
  const t0 = Date.now() - 2 * HOUR;
  spend.recordCodexCreditBalance(id, 1000, t0);
  spend.recordCodexCreditBalance(id, 900, t0 + 10_000);
  spend.recordCodexCreditBalance(id, 950, t0 + 20_000); // purchase
  spend.recordCodexCreditBalance(id, 10, t0 + 15_000); // stale, out of order
  spend.recordCodexCreditBalance(id, 800, t0 + 30_000);
  const observed = spend.getCodexCreditSpend(id)!;
  assert.equal(observed.day, 250);
  assert.equal(observed.week, 250);
  assert.equal(observed.trackedSince, t0);
});

test("daily and weekly windows roll independently and old drops expire", () => {
  const id = `spend-rolling-${Date.now()}`;
  const now = Date.now();
  spend.recordCodexCreditBalance(id, 1000, now - 8 * 24 * HOUR);
  spend.recordCodexCreditBalance(id, 900, now - 8 * 24 * HOUR + 1); // 100, older than 7 days
  spend.recordCodexCreditBalance(id, 700, now - 48 * HOUR); // 200, weekly only
  spend.recordCodexCreditBalance(id, 650, now - HOUR); // 50, both
  assert.deepEqual(
    { day: spend.getCodexCreditSpend(id, now)!.day, week: spend.getCodexCreditSpend(id, now)!.week },
    { day: 50, week: 250 }
  );
});

test("credit admission stops at a reached limit and fails closed when consumption is unknown", () => {
  const quota = { used: 100, total: 100, percentUsed: 1, observedAt: Date.now(), limitReached: true,
    windows: { session: { percentUsed: 1, resetAt: new Date(Date.now() + 100 * HOUR).toISOString() } },
    paidCredits: { hasCredits: true, unlimited: false, overageLimitReached: false, balance: 5000 } };
  const scope = (data: Record<string, unknown>, used?: { day: number; week: number } | null) => ({
    provider: "codex", requestedModel: "gpt-6.1-sol",
    providerSpecificData: { allowPaidCredits: true, ...data },
    codexCreditSpend: used === undefined ? undefined : used && { ...used, trackedSince: 0 },
  });
  assert.equal(evaluateQuotaCutoff(quota, undefined, scope({})).billingRung, "credits", "no limit needs no tracker");
  assert.equal(evaluateQuotaCutoff(quota, undefined, scope({ creditDailyLimit: 100 }, { day: 99, week: 99 })).billingRung, "credits");
  const daily = evaluateQuotaCutoff(quota, undefined, scope({ creditDailyLimit: 100 }, { day: 100, week: 100 }));
  assert.equal(daily.proceed, false);
  assert.equal(daily.reason, "credit_limit_reached");
  assert.deepEqual(daily.creditLimit, { window: "daily", limit: 100, spent: 100 });
  const weekly = evaluateQuotaCutoff(quota, undefined, scope({ creditDailyLimit: 1000, creditWeeklyLimit: 300 }, { day: 10, week: 300 }));
  assert.equal(weekly.reason, "credit_limit_reached");
  assert.equal(weekly.creditLimit?.window, "weekly");
  const unknown = evaluateQuotaCutoff(quota, undefined, scope({ creditWeeklyLimit: 300 }));
  assert.equal(unknown.proceed, false);
  assert.equal(unknown.reason, "credit_limit_reached");
  const consentOff = evaluateQuotaCutoff(quota, undefined, { ...scope({ creditDailyLimit: 100 }, { day: 0, week: 0 }),
    providerSpecificData: { allowPaidCredits: false, creditDailyLimit: 100 } });
  assert.equal(consentOff.reason, "quota_exhausted", "a limit never grants consent");
});

test("limits do not block included plan allowance", () => {
  const quota = { used: 20, total: 100, percentUsed: 0.2, observedAt: Date.now(),
    windows: { session: { percentUsed: 0.2, resetAt: new Date(Date.now() + HOUR).toISOString() } } };
  const decision = evaluateQuotaCutoff(quota, undefined, { provider: "codex", requestedModel: "gpt-6.1-sol",
    providerSpecificData: { allowPaidCredits: true, creditDailyLimit: 1 }, codexCreditSpend: { day: 99, week: 99, trackedSince: 0 } });
  assert.equal(decision.billingRung, "included");
});

test("real Codex usage observations trip a daily limit on the next admission", async () => {
  registerCodexQuotaFetcher();
  const connectionId = `codex-credit-limit-${Date.now()}`;
  let balance = 1000;
  globalThis.fetch = async () => Response.json({
    rate_limit: { limit_reached: true, primary_window: { used_percent: 100, reset_after_seconds: 360000 } },
    credits: { has_credits: true, unlimited: false, overage_limit_reached: false, balance: String(balance) },
  });
  const connection = { accessToken: "credit-limit-fixture", requestedModel: "gpt-6.1-sol",
    providerSpecificData: { allowPaidCredits: true, creditDailyLimit: 100 } };
  try {
    assert.equal((await preflightQuota("codex", connectionId, connection)).billingRung, "credits");
    invalidateCodexQuotaCache(connectionId);
    balance = 960;
    await delay(5);
    assert.equal((await preflightQuota("codex", connectionId, connection)).billingRung, "credits", "40 of 100 used");
    invalidateCodexQuotaCache(connectionId);
    balance = 890;
    await delay(5);
    const blocked = await preflightQuota("codex", connectionId, connection);
    assert.equal(blocked.proceed, false);
    assert.equal(blocked.reason, "credit_limit_reached");
    assert.equal(blocked.creditLimit?.spent, 110);
  } finally { invalidateCodexQuotaCache(connectionId); }
});

test("saved limits keep only positive numbers and null clears", () => {
  assert.deepEqual(normalizeProviderSpecificData("codex", { allowPaidCredits: true, creditDailyLimit: 500, creditWeeklyLimit: 2000 }),
    { allowPaidCredits: true, creditDailyLimit: 500, creditWeeklyLimit: 2000 });
  assert.deepEqual(normalizeProviderSpecificData("codex", { allowPaidCredits: true, creditDailyLimit: null, creditWeeklyLimit: -1 }),
    { allowPaidCredits: true });
  assert.equal(normalizeProviderSpecificData("codex", { creditDailyLimit: "500" })?.creditDailyLimit, undefined,
    "strings are not limits");
});

test("manual quota refresh returns the newly observed credit spend for provider-page graphs", async () => {
  const { createProviderConnection } = await import("../../src/lib/db/providers.ts");
  const { GET } = await import("../../src/app/api/usage/[connectionId]/route.ts");
  const account = await createProviderConnection({
    provider: "codex", authType: "oauth", name: "credit-visual-refresh",
    accessToken: `credit-visual-refresh-${Date.now()}`,
    tokenExpiresAt: new Date(Date.now() + HOUR).toISOString(), isActive: true,
    providerSpecificData: { allowPaidCredits: false },
  });
  spend.recordCodexCreditBalance(account.id, 1000, Date.now() - 1000);
  globalThis.fetch = async () => Response.json({
    rate_limit: { primary_window: { used_percent: 20, reset_after_seconds: 3600 } },
    credits: { has_credits: true, unlimited: false, overage_limit_reached: false, balance: "800" },
  });
  const response = await GET(new Request(`http://localhost/api/usage/${account.id}`), {
    params: Promise.resolve({ connectionId: account.id }),
  });
  assert.equal(response.status, 200);
  const usage = await response.json();
  assert.equal(usage.paidCredits.balance, 800);
  assert.equal(usage.creditSpend.day, 200);
  assert.equal(usage.creditSpend.week, 200);
});
