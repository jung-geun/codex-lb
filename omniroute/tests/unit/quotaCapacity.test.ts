import test from "node:test";
import assert from "node:assert/strict";
import { estimateQuotaCapacity } from "../../src/lib/services/quotaCapacity.ts";

const now = Date.parse("2026-10-21T12:00:00.000Z");
const reset = now + 3_600_000;
const left = now - 1_200_000;

function observation(at: number, usedRatio: number, resetsAt = reset) {
  return { observedAt: at, usedRatio, resetsAt };
}

function request(overrides: Partial<{
  startedAt: number | null;
  completedAt: number;
  model: string | null;
  inputTokens: number | null;
  outputTokens: number | null;
  success: boolean;
  reported: boolean;
}> = {}) {
  return {
    startedAt: now - 610_000,
    completedAt: now - 600_000,
    model: "claude-sonnet",
    inputTokens: 800,
    outputTokens: 200,
    success: true,
    reported: true,
    ...overrides,
  };
}

function estimate(observations = [observation(left, 0.2), observation(now, 0.3)], requests = [request()]) {
  return estimateQuotaCapacity({ windowKey: "session", observations, requests, now });
}

test("estimates capacity from confirmed input plus output within a 5-point interval", () => {
  const result = estimate();
  assert.equal(result.status, "estimated");
  assert.equal(result.estimateKind, "empirical_estimate");
  assert.equal(result.observedTokens, 1000);
  assert.ok(Math.abs(result.estimatedTotalTokens - 10_000) < 1e-8);
  assert.ok(Math.abs(result.remainingTokens - 7_000) < 1e-8);
});

test("requires a current snapshot no older than five minutes and a future reset", () => {
  assert.equal(estimate([observation(now - 300_001, 0.2), observation(now - 300_001, 0.3)]).reason, "current_stale");
  assert.equal(estimate([observation(left, 0.2, now - 1), observation(now, 0.3, now - 1)]).reason, "reset_elapsed");
});

test("accepts exactly five percentage points and a snapshot exactly five minutes old", () => {
  const result = estimate([observation(left, 0.2), observation(now - 300_000, 0.25)]);
  assert.equal(result.status, "estimated");
  assert.ok(Math.abs(result.estimatedTotalTokens - 20_000) < 1e-8);
});

test("does not combine observations from different resets or sub-five-point deltas", () => {
  assert.equal(estimate([observation(left, 0.2, reset - 1), observation(now, 0.3)]).reason, "insufficient_delta");
  assert.equal(estimate([observation(left, 0.2), observation(now, 0.249)]).reason, "insufficient_delta");
});

test("rejects boundary ambiguity above ten percent without prorating calls", () => {
  const crossing = request({
    startedAt: left - 1,
    completedAt: left + 1,
    inputTokens: 500,
    outputTokens: 0,
  });
  assert.equal(estimate(undefined, [request(), crossing]).reason, "boundary_overlap");
});

test("accepts boundary uncertainty at exactly ten percent", () => {
  const crossing = request({ startedAt: left, completedAt: left + 1, inputTokens: 100, outputTokens: 0 });
  const result = estimate(undefined, [request(), crossing]);
  assert.equal(result.status, "estimated");
  assert.ok(Math.abs(result.totalTokensHigh - 11_000) < 1e-8);
  assert.equal(result.boundaryUncertaintyTokens, 100);
});

test("missing usage remains a recorded lower bound with a null upper range", () => {
  const partial = request({
    completedAt: now - 300_000,
    startedAt: now - 301_000,
    inputTokens: null,
    outputTokens: null,
    reported: false,
  });
  const result = estimate(undefined, [request(), partial]);
  assert.equal(result.estimateKind, "recorded_lower_bound");
  assert.equal(result.totalTokensHigh, null);
  assert.equal(result.observedTokens, 1000);
  assert.ok(Math.abs(result.estimatedTotalTokens - 10_000) < 1e-8);
});

test("unknown call timing preserves only a recorded lower bound", () => {
  const unknown = request({ completedAt: now - 1_000, startedAt: null });
  const result = estimate(undefined, [request(), unknown]);
  assert.equal(result.estimateKind, "recorded_lower_bound");
  assert.equal(result.reason, "incomplete_usage");
  assert.equal(result.totalTokensHigh, null);
});

test("an unsupported identity or scope never exposes an estimated capacity", () => {
  const result = estimateQuotaCapacity({
    windowKey: "model-sonnet",
    observations: [observation(left, 0.2), observation(now, 0.3)],
    unavailableReason: "unsupported_scope",
  });
  assert.equal(result.status, "unknown");
  assert.equal(result.reason, "unsupported_scope");
  assert.equal(result.estimatedTotalTokens, null);
});
