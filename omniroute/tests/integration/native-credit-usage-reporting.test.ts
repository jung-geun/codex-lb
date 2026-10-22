import test from "node:test";
import assert from "node:assert/strict";
import { setImmediate as drainMicrotasks } from "node:timers/promises";
import { createChatPipelineHarness } from "./_chatPipelineHarness.ts";

const harness = await createChatPipelineHarness("native-credit-usage");
// These runtime modules must load after the harness binds the isolated DATA_DIR.
const providers = await import("../../src/lib/db/providers.ts");
const models = await import("../../src/lib/db/models.ts");
const settings = await import("../../src/lib/db/settings.ts");
const { updatePricing } = await import("../../src/lib/db/settings/pricing.ts");
const { invalidateDbCache } = await import("../../src/lib/db/readCache.ts");
const { handleChat } = await import("../../src/sse/handlers/chat.ts");
const costRules = await import("../../src/domain/costRules.ts");
const ledger = await import("../../src/lib/db/costLedger.ts");
const { GET: getBudget } = await import("../../src/app/api/usage/budget/route.ts");
const { GET: getHistory } = await import("../../src/app/api/usage/history/route.ts");
const { GET: getAnalytics } = await import("../../src/app/api/usage/analytics/route.ts");
const { clearAnalyticsResponseCache } = await import("../../src/lib/usage/analyticsResponseCache.ts");
const { flushProxyLogsSync } = await import("../../src/lib/proxyLogger.ts");
const { closeCallLogArtifactWriter } = await import("../../src/lib/usage/callLogArtifactWriter.ts");
const { saveRequestUsage, getUsageHistory } = await import("../../src/lib/usage/usageHistory.ts");
const { cleanupUsageHistory } = await import("../../src/lib/db/cleanup.ts");
const { updateDatabaseSettings } = await import("../../src/lib/db/databaseSettings.ts");
const model = "claude-opus-5-5";

test.beforeEach(async () => {
  flushProxyLogsSync();
  costRules.resetCostData();
  clearAnalyticsResponseCache();
  await harness.resetStorage();
});
test.after(async () => {
  await closeCallLogArtifactWriter();
  flushProxyLogsSync();
  await harness.cleanup();
});

function nativeCompletion(stream: boolean) {
  if (!stream) return Response.json({
    id: "msg_credit", type: "message", role: "assistant", model,
    content: [{ type: "text", text: "NATIVE_OK" }], stop_reason: "end_turn", stop_sequence: null,
    usage: { input_tokens: 1000, output_tokens: 100 },
  });
  const events = [
    { type: "message_start", message: { id: "msg_credit", type: "message", role: "assistant", model,
      content: [], usage: { input_tokens: 1000, output_tokens: 0 } } },
    { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } },
    { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "NATIVE_OK" } },
    { type: "content_block_stop", index: 0 },
    { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 100 } },
    { type: "message_stop" },
  ];
  return new Response(events.map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(""),
    { headers: { "Content-Type": "text/event-stream" } });
}

function codexCompletion(model: string) {
  const message = { id: "msg_credit", type: "message", role: "assistant", status: "completed",
    content: [{ type: "output_text", text: "NATIVE_OK", annotations: [] }] };
  const response = { id: "resp_credit", object: "response", status: "completed", model,
    output: [message], output_text: "NATIVE_OK", usage: { input_tokens: 1000, output_tokens: 100,
      total_tokens: 1100, input_tokens_details: { cached_tokens: 0 }, output_tokens_details: { reasoning_tokens: 0 } } };
  const events = [
    { type: "response.created", response: { ...response, status: "in_progress", output: [], output_text: "", usage: null } },
    { type: "response.output_item.added", output_index: 0, item: { ...message, status: "in_progress", content: [] } },
    { type: "response.content_part.added", output_index: 0, content_index: 0, item_id: message.id,
      part: { type: "output_text", text: "", annotations: [] } },
    { type: "response.output_text.delta", output_index: 0, content_index: 0, item_id: message.id, delta: "NATIVE_OK" },
    { type: "response.output_text.done", output_index: 0, content_index: 0, item_id: message.id, text: "NATIVE_OK" },
    { type: "response.output_item.done", output_index: 0, item: message },
    { type: "response.completed", response },
  ];
  return new Response(events.map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(""),
    { headers: { "Content-Type": "text/event-stream" } });
}

for (const [provider, requestedModel] of [["claude", model], ["codex", "gpt-6.1-sol"]] as const) {

for (const stream of [false, true]) {
  for (const rung of ["included", "credits"] as const) {
    test(`real native ${provider} ${stream ? "stream" : "JSON"} ${rung} reports budget estimates separately from unknown charges`, async () => {
      const key = await harness.seedApiKey();
      const account = await providers.createProviderConnection({
        provider, authType: "oauth", name: `native-${provider}-${rung}-${stream}`,
        // Claude's authoritative usage cache is credential-keyed, not DATA_DIR-keyed.
        // Separate synthetic accounts must not inherit another rung's observation.
        accessToken: `synthetic-native-access-${key.id}`, refreshToken: `synthetic-native-refresh-${key.id}`,
        tokenExpiresAt: new Date(Date.now() + 3600_000).toISOString(), isActive: true, testStatus: "active",
        providerSpecificData: provider === "codex" ? { allowPaidCredits: true } : { blockExtraUsage: false },
      });
      await models.replaceSyncedAvailableModelsForConnection(provider, account.id, [{ id: requestedModel, name: requestedModel }]);
      await settings.updateSettings({ nativeSubscriptionRouting: { enabled: true }, requestRetry: 0, maxRetryIntervalSec: 0 });
      await updatePricing({ [provider]: { [requestedModel]: { input: 10, output: 20 } } });
      costRules.setBudget(key.id, { dailyLimitUsd: rung === "included" ? 0.01 : 100, resetInterval: "daily" });
      // A spent metered allowance cannot take healthy included subscription capacity down.
      if (rung === "included") costRules.recordCost(key.id, 1);
      invalidateDbCache();
      let inferenceCalls = 0;
      let usageReads = 0;
      globalThis.fetch = async (input) => {
        if (String(input).includes(provider === "codex" ? "/responses" : "/v1/messages")) {
          inferenceCalls++;
          return provider === "codex" ? codexCompletion(requestedModel) : nativeCompletion(stream);
        }
        if (String(input).includes(provider === "codex" ? "/wham/usage" : "/oauth/usage")) usageReads++;
        if (provider === "codex") return Response.json({
          rate_limit: { allowed: rung === "included", limit_reached: rung === "credits",
            primary_window: { used_percent: rung === "credits" ? 100 : 20, reset_at: Math.floor(Date.now() / 1000) + 3600, limit_window_seconds: 18000 },
            secondary_window: { used_percent: 20, reset_at: Math.floor(Date.now() / 1000) + 86400, limit_window_seconds: 604800 } },
          credits: { has_credits: true, unlimited: false, overage_limit_reached: false, balance: "20" },
        });
        return Response.json({
          five_hour: { utilization: rung === "credits" ? 100 : 20,
            resets_at: new Date(Date.now() + 3600_000).toISOString() },
          seven_day: { utilization: 20, resets_at: new Date(Date.now() + 86400_000).toISOString() },
          extra_usage: { is_enabled: true, monthly_limit: 1000, used_credits: 200 },
        });
      };
      const response = await handleChat(new Request(`http://localhost/v1/${provider === "codex" ? "responses" : "messages"}`, {
        method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${key.key}` },
        body: JSON.stringify({ model: `${provider}/${requestedModel}`, stream,
          ...(provider === "codex" ? { input: "Return NATIVE_OK", max_output_tokens: 128 }
            : { max_tokens: 128, messages: [{ role: "user", content: "Return NATIVE_OK" }] }) }),
      }));
      const body = await response.text();
      assert.equal(response.status, 200, body);
      assert.match(body, /NATIVE_OK/);
      assert.equal(response.headers.get("x-omniroute-selected-connection-id"), account.id);
      assert.equal(inferenceCalls, 1);
      assert.ok(usageReads > 0, "admission must use this fixture's authoritative quota observation");
      // Drain completion-hook promise chains before observing durable reporting.
      await drainMicrotasks();
      const entries = ledger.listLedgerEntries(key.id);
      const expectedEstimate = (1000 * 10 + 100 * 20) / 1_000_000;
      assert.equal(entries.length, rung === "credits" ? 1 : 0);
      if (rung === "credits") {
        const [entry] = entries;
        assert.equal(entry.provider, provider);
        assert.equal(entry.model, requestedModel);
        assert.equal(entry.tokensInput, 1000);
        assert.equal(entry.tokensOutput, 100);
        assert.equal(entry.unitPriceInput, 10);
        assert.equal(entry.unitPriceOutput, 20);
        assert.equal(entry.serviceTier, "standard");
        assert.equal(entry.success, true);
        assert.ok(entry.requestId);
        assert.ok(Math.abs(entry.amountUsd - expectedEstimate) < 1e-12);
      }
      const budgetResponse = await getBudget(new Request(`http://localhost/api/usage/budget?apiKeyId=${key.id}`));
      assert.equal(budgetResponse.status, 200);
      const budget = await budgetResponse.json();
      const historyResponse = await getHistory(new Request("http://localhost/api/usage/history"));
      assert.equal(historyResponse.status, 200);
      const history = await historyResponse.json();
      const analyticsResponse = await getAnalytics(new Request(`http://localhost/api/usage/analytics?range=30d&apiKeyIds=${key.id}`));
      assert.equal(analyticsResponse.status, 200);
      const analytics = await analyticsResponse.json();
      for (const view of [budget.ledgerDebits, history.nativeBudgetDebits, analytics.nativeBudgetDebits]) {
        assert.equal(view.costBasis, "api_equivalent_budget_debit");
        assert.equal(view.coverage, "retained_cost_ledger_only");
        assert.equal(view.actualChargeUsd, null);
        assert.equal(view.billingRungAttribution, "unavailable");
        assert.equal(view.requestCount, rung === "credits" ? 1 : 0);
        if (rung === "credits") {
          assert.ok(Math.abs(view.amountUsd - expectedEstimate) < 1e-12);
          assert.equal(view.byProvider[0].provider, provider);
        } else assert.equal(view.amountUsd, 0);
      }
      assert.equal(history.nativeActualChargeUsd, null);
      assert.ok(history.nativeUsageRequests > 0);
      assert.equal(analytics.nativeActualChargeUsd, null);
      assert.equal(analytics.byProvider.length, 1);
      const nativeRow = analytics.byProvider[0];
      assert.ok(nativeRow.unknownCostRequests > 0);
      if (rung === "credits") {
        assert.ok(Math.abs(budget.totalCostToday - expectedEstimate) < 1e-12);
        assert.equal(budget.ledgerDebits.amountUsd, entries[0].amountUsd);
      } else {
        assert.equal(budget.totalCostToday, 1, "included inference must not add any debit to the already spent allowance");
        assert.deepEqual(entries, []);
      }
      costRules.deleteBudget(key.id);
    });
  }
}
}

test("a spent Codex API-key budget rejects before inference without probing subscription quota", async () => {
  const key = await harness.seedApiKey();
  const apiModel = "gpt-6.1-sol";
  const account = await providers.createProviderConnection({
    provider: "codex", authType: "apikey", name: "metered-codex-api",
    apiKey: "synthetic-api-credential", isActive: true, testStatus: "active",
  });
  await models.replaceSyncedAvailableModelsForConnection("codex", account.id, [{ id: apiModel, name: apiModel }]);
  await settings.updateSettings({ nativeSubscriptionRouting: { enabled: false }, requestRetry: 0 });
  costRules.setBudget(key.id, { dailyLimitUsd: 1, resetInterval: "daily" });
  costRules.recordCost(key.id, 2);
  invalidateDbCache();
  let upstreamCalls = 0;
  globalThis.fetch = async () => { upstreamCalls++; throw new Error("No upstream work is allowed for this spent API budget"); };
  const response = await handleChat(new Request("http://localhost/v1/responses", {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${key.key}` },
    body: JSON.stringify({ model: `codex/${apiModel}`, input: "Budget refusal fixture", stream: false }),
  }));
  assert.equal(response.status, 429);
  assert.match(await response.text(), /BUDGET_EXCEEDED/);
  assert.equal(upstreamCalls, 0);
  assert.equal(costRules.getCostSummary(key.id).totalCostToday, 2);
  costRules.deleteBudget(key.id);
});

test("retained ledger view does not invent missing estimates or infer historical credit usage", () => {
  ledger.recordLedgerEntries([
    { apiKeyId: "own", provider: "codex", model: "historical", amountUsd: 0.4, timestamp: "2026-10-10T00:00:00.000Z" },
    { apiKeyId: "other", provider: "claude", model: "private", amountUsd: 99, timestamp: "2026-10-10T00:00:00.000Z" },
    { apiKeyId: "own", provider: "openai", model: "api", amountUsd: 2, timestamp: "2026-10-10T00:00:00.000Z" },
  ]);
  const view = ledger.getLedgerBudgetBreakdown({ apiKeyIds: ["own"], nativeOnly: true,
    sinceIso: "2026-10-01T00:00:00.000Z", untilIso: "2026-10-22T00:00:00.000Z" });
  assert.equal(view.amountUsd, 0.4);
  assert.deepEqual(view.byProvider, [{ provider: "codex", amountUsd: 0.4, requestCount: 1 }]);
  assert.equal(view.actualChargeUsd, null);
  assert.equal(view.billingRungAttribution, "unavailable");
  const missing = ledger.getLedgerBudgetBreakdown({ apiKeyIds: ["no-estimate"] });
  assert.equal(missing.requestCount, 0);
  assert.equal(missing.actualChargeUsd, null);
  assert.equal(missing.coverage, "retained_cost_ledger_only");
});

test("priced raw Codex usage is excluded from the non-native total, not relabeled as invoice cost", async () => {
  await updatePricing({ codex: { "gpt-6.1-sol": { input: 10, output: 20 } } });
  await saveRequestUsage({ provider: "codex", model: "gpt-6.1-sol", tokens: { input: 1000, output: 100 },
    apiKeyId: "raw", status: "200", success: true });
  ledger.recordLedgerEntry({ apiKeyId: "raw", provider: "codex", model: "gpt-6.1-sol", amountUsd: 0.25 });
  const response = await getHistory(new Request("http://localhost/api/usage/history"));
  assert.equal(response.status, 200);
  const history = await response.json();
  assert.equal(history.totalCost, 0);
  assert.equal(history.byProvider.codex.cost, 0);
  assert.equal(history.byModel["gpt-6.1-sol (codex)"].cost, 0);
  assert.equal(history.nativeBudgetDebits.amountUsd, 0.25);
  assert.equal(history.nativeActualChargeUsd, null);
});

test("usage fold and prune preserve the separate retained native budget evidence", async () => {
  updateDatabaseSettings({ aggregation: { enabled: true, rawDataRetentionDays: 7, granularity: "daily" } });
  await updatePricing({ codex: { "gpt-6.1-sol": { input: 10, output: 20 } } });
  await saveRequestUsage({ provider: "codex", model: "gpt-6.1-sol", tokens: { input: 1000, output: 100 },
    timestamp: "2000-01-01T00:00:00.000Z", apiKeyId: "folded", status: "200", success: true });
  ledger.recordLedgerEntry({ apiKeyId: "folded", provider: "codex", model: "gpt-6.1-sol",
    amountUsd: 0.25, timestamp: "2000-01-01T00:00:00.000Z" });
  const result = await cleanupUsageHistory();
  assert.equal(result.errors, 0);
  assert.equal(result.deleted, 1);
  assert.equal((await getUsageHistory()).length, 0);
  const response = await getHistory(new Request("http://localhost/api/usage/history"));
  const history = await response.json();
  assert.equal(history.totalCost, 0);
  assert.equal(history.byProvider.codex.cost, 0);
  assert.equal(history.byModel["gpt-6.1-sol (codex)"].cost, 0);
  assert.equal(history.nativeUsageRequests, 1);
  assert.equal(history.nativeBudgetDebits.amountUsd, 0.25);
  assert.equal(history.nativeBudgetDebits.requestCount, 1);
  assert.equal(history.nativeActualChargeUsd, null);
});
