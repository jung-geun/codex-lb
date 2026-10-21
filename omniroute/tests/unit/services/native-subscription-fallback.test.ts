import { test } from "vitest";
import assert from "node:assert/strict";

import {
  isNativeSubmissionAmbiguous,
  selectNativePaidFallbacks,
} from "@omniroute/open-sse/services/combo/nativeSubscriptionFallback.ts";
import { ConnectionQuotaHysteresis } from "@omniroute/open-sse/services/autoCombo/subscriptionLadder.ts";
import type { FreeAccessState } from "@omniroute/open-sse/services/autoCombo/strictZeroCostFilter.ts";

const primary = { providerFamily: "openai", upstreamModel: "gpt-6.1" };
const route = (overrides: Partial<NativeFallbackRoute> = {}): NativeFallbackRoute => ({
  providerFamily: "openai",
  upstreamModel: "gpt-6.1",
  connectionId: "api-1",
  billing: "api",
  ...overrides,
});

test("ambiguous native dispatch is terminal and cannot enter fallback", () => {
  assert.equal(isNativeSubmissionAmbiguous("chatgpt_submission_ambiguous"), true);
  assert.equal(isNativeSubmissionAmbiguous("chatgpt_submitted_turn_failed"), false);
  assert.equal(isNativeSubmissionAmbiguous("upstream_timeout"), false);
});

test("native paid fallback requires same provider family and exact upstream model", () => {
  const exact = route();
  const wrongFamily = route({ connectionId: "api-2", providerFamily: "anthropic" });
  const wrongModel = route({ connectionId: "api-3", upstreamModel: "gpt-6.1-mini" });

  assert.deepEqual(selectNativePaidFallbacks(primary, [exact, wrongFamily, wrongModel], {
    subscriptionOnly: false,
  }), [exact]);
});

test("subscription-only and API-key connection restrictions fail closed", () => {
  const exact = route();
  assert.deepEqual(
    selectNativePaidFallbacks(primary, [exact], { subscriptionOnly: true }),
    []
  );
  assert.deepEqual(
    selectNativePaidFallbacks(primary, [exact], {
      subscriptionOnly: false,
      allowedConnectionIds: ["different-key-connection"],
    }),
    []
  );
  assert.deepEqual(
    selectNativePaidFallbacks(primary, [exact], {
      subscriptionOnly: false,
      allowedConnectionIds: [],
    }),
    []
  );
});

test("non-API routes and API routes without connection identity are never paid fallbacks", () => {
  assert.deepEqual(
    selectNativePaidFallbacks(
      primary,
      [route({ billing: "subscription" }), route({ connectionId: null })],
      { subscriptionOnly: false }
    ),
    []
  );
});

test("exhaustion hysteresis is per connection and clears only at a proven reset", () => {
  const tracker = new ConnectionQuotaHysteresis();
  const resetAt = "2026-10-21T12:00:00.000Z";
  const state = (overrides: Partial<FreeAccessState> = {}): FreeAccessState => ({
    status: "EXHAUSTED",
    remainingFreeAllowance: 0,
    resetAt,
    checkedAt: "2026-10-21T11:00:00.000Z",
    ...overrides,
  });

  assert.equal(tracker.hasBeenExhausted("conn-a", state(), Date.parse("2026-10-21T11:30:00Z")), true);
  assert.equal(
    tracker.hasBeenExhausted(
      "conn-a",
      state({ status: "SAFE", remainingFreeAllowance: 3 }),
      Date.parse("2026-10-21T11:40:00Z"),
      2
    ),
    true,
    "a low remaining value cannot re-enter after exhaustion"
  );
  assert.equal(
    tracker.hasBeenExhausted(
      "conn-b",
      state({ status: "SAFE", remainingFreeAllowance: 3 }),
      Date.parse("2026-10-21T11:40:00Z"),
      2
    ),
    false,
    "a different connection has independent history"
  );
  assert.equal(
    tracker.hasBeenExhausted(
      "conn-a",
      state({ status: "SAFE", remainingFreeAllowance: 80 }),
      Date.parse("2026-10-21T11:50:00Z"),
      2
    ),
    true,
    "headroom alone does not clear exhaustion before reset"
  );
  assert.equal(
    tracker.hasBeenExhausted(
      "conn-a",
      state({ status: "SAFE", remainingFreeAllowance: 80, checkedAt: resetAt }),
      Date.parse("2026-10-21T12:00:01Z"),
      2
    ),
    false,
    "the upstream reset instant clears the prior window"
  );
});
