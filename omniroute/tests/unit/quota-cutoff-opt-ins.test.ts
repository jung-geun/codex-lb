import test from "node:test";
import assert from "node:assert/strict";
import {
  defersQuotaCutoff,
} from "../../src/lib/providers/quotaCutoffOptIns.ts";
import { isCodexPaidCreditsEnabled } from "../../src/lib/providers/codexPaidCredits.ts";
import { evaluateQuotaCutoff } from "../../open-sse/services/quotaPreflight.ts";

test("Claude extra usage defers the subscription cutoff only when explicitly allowed", () => {
  assert.equal(defersQuotaCutoff("claude", { blockExtraUsage: false }), true);
  assert.equal(defersQuotaCutoff("claude", { blockExtraUsage: true }), false);
  assert.equal(defersQuotaCutoff("claude", {}), false);
});

test("Codex paid credits defer the subscription cutoff except for Spark", () => {
  const optedIn = { allowPaidCredits: true };
  assert.equal(defersQuotaCutoff("codex", optedIn, "codex/gpt-5.5"), true);
  assert.equal(defersQuotaCutoff("codex", optedIn, "codex/gpt-5.3-codex-spark"), false);
  assert.equal(defersQuotaCutoff("codex", { allowPaidCredits: false }, "codex/gpt-5.5"), false);
  assert.equal(defersQuotaCutoff("openai", optedIn, "gpt-5.5"), false);
});


// #13337 — paid Codex credits can incur charges, so the behavior is default-OFF: only an explicit
// per-connection `allowPaidCredits: true` turns it on.
test("Codex paid credits are disabled by default and only an explicit boolean true enables them", () => {
  assert.equal(isCodexPaidCreditsEnabled("codex", undefined, "codex/gpt-5.5"), false);
  assert.equal(isCodexPaidCreditsEnabled("codex", null, "codex/gpt-5.5"), false);
  assert.equal(isCodexPaidCreditsEnabled("codex", {}, "codex/gpt-5.5"), false);
  assert.equal(
    isCodexPaidCreditsEnabled("codex", { allowPaidCredits: "true" }, "codex/gpt-5.5"),
    false
  );
  assert.equal(isCodexPaidCreditsEnabled("codex", { allowPaidCredits: 1 }, "codex/gpt-5.5"), false);
  assert.equal(
    isCodexPaidCreditsEnabled("codex", { allowPaidCredits: true }, "codex/gpt-5.5"),
    true
  );
  // Unknown included-plan state is not permission to let upstream bill a native request.
  assert.deepEqual(
    evaluateQuotaCutoff(null, undefined, {
      provider: "codex",
      requestedModel: "codex/gpt-5.5",
      providerSpecificData: {},
    }),
    { proceed: false, reason: "quota_unavailable" }
  );
});

test("the Codex safety opt-out neither erases explicit credit consent nor invents it", () => {
  const safetyOptOut = { quotaPreflightEnabled: false, limitPolicy: { enabled: false } };
  const scope = { provider: "codex", requestedModel: "codex/gpt-5.5", providerSpecificData: safetyOptOut };
  const exhausted = { observedAt: Date.now(), used: 100, total: 100, percentUsed: 1, limitReached: true,
    paidCredits: { hasCredits: true, unlimited: false, overageLimitReached: false, balance: 1000 } };
  assert.equal(isCodexPaidCreditsEnabled("codex", safetyOptOut, scope.requestedModel), false);
  assert.equal(evaluateQuotaCutoff(exhausted, undefined, scope).proceed, false);
  const explicitConsent = { ...safetyOptOut, allowPaidCredits: true };
  assert.equal(isCodexPaidCreditsEnabled("codex", explicitConsent, scope.requestedModel), true);
  assert.equal(evaluateQuotaCutoff(exhausted, undefined, { ...scope, providerSpecificData: explicitConsent }).billingRung, "credits");
  assert.equal(evaluateQuotaCutoff(null, undefined, { ...scope, providerSpecificData: explicitConsent }).proceed, false);
});
