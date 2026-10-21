import { getQuotaCapacityEvidence } from "@/lib/db/quotaCapacity";
import { getProviderLimitsCache } from "@/lib/db/providerLimits";

export type CapacityReason =
  | "valid"
  | "no_observations"
  | "unsupported_scope"
  | "identity_ambiguous"
  | "missing_reset"
  | "current_stale"
  | "current_future"
  | "reset_elapsed"
  | "insufficient_delta"
  | "incomplete_usage"
  | "boundary_overlap"
  | "inconsistent_observations"
  | "no_matched_usage";

export interface CapacityEstimate {
  windowKey: string;
  status: "estimated" | "insufficient" | "unknown";
  reason: CapacityReason;
  estimateKind: "empirical_estimate" | "recorded_lower_bound" | null;
  estimatedTotalTokens: number | null;
  remainingTokens: number | null;
  totalTokensLow: number | null;
  totalTokensHigh: number | null;
  observedTokens: number;
  boundaryUncertaintyTokens: number;
  sampleCount: number;
  observationCount: number;
  requestCount: number;
  usedRatioDelta: number;
  currentObservedAt: string | null;
  currentUsedRatio: number | null;
  resetsAt: string | null;
  evidenceStart: string | null;
  evidenceEnd: string | null;
  basis: "observed_gateway_input_plus_output_tokens";
  caveats: string[];
}

interface Observation {
  observedAt: number;
  usedRatio: number | null;
  resetsAt: number | null;
}

interface Usage {
  startedAt: number | null;
  completedAt: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
  success: boolean;
  reported: boolean;
}

const DAY = 86_400_000;
const CAVEATS = [
  "Empirical gateway-observed token equivalent, not an official provider token cap or entitlement.",
  "Outside-gateway usage and missing retained logs can bias the estimate downward; no guaranteed bound.",
  "Model, cache, reasoning and provider weighting can change token equivalents; future mix may differ.",
  "Provider rounding, reporting delay and unlogged in-flight requests cannot be reconstructed.",
  "Uses normalized input plus output once; cache and reasoning subtotals are not added again.",
  "Range includes interval variation and boundary-token allowance, not a statistical confidence interval.",
  "Boundary-spanning usage is bounded zero-to-full, not time-prorated; allowance is at most 10% of recorded tokens.",
];

const validCount = (value: number | null): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const validRatio = (value: number | null): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;

function makeUnknown(windowKey: string, reason: CapacityReason): CapacityEstimate {
  return {
    windowKey,
    status: "unknown",
    reason,
    estimateKind: null,
    estimatedTotalTokens: null,
    remainingTokens: null,
    totalTokensLow: null,
    totalTokensHigh: null,
    observedTokens: 0,
    boundaryUncertaintyTokens: 0,
    sampleCount: 0,
    observationCount: 0,
    requestCount: 0,
    usedRatioDelta: 0,
    currentObservedAt: null,
    currentUsedRatio: null,
    resetsAt: null,
    evidenceStart: null,
    evidenceEnd: null,
    basis: "observed_gateway_input_plus_output_tokens",
    caveats: [...CAVEATS],
  };
}

function bound(values: number[], at: number, inclusive: boolean): number {
  let lo = 0, hi = values.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (values[mid] < at || (inclusive && values[mid] === at)) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** Prefix sums keep retained request volume out of the per-observation loop. */
function indexUsage(requests: Usage[]) {
  const complete = requests.filter((r) => r.completedAt !== null && r.startedAt !== null && r.startedAt <= r.completedAt)
    .sort((a, b) => a.completedAt - b.completedAt);
  const ends = complete.map((r) => r.completedAt);
  const startsOrdered = [...complete].sort((a, b) => a.startedAt - b.startedAt);
  const starts = startsOrdered.map((r) => r.startedAt);
  const amount = (r: Usage) => r.success && r.reported && validCount(r.inputTokens) && validCount(r.outputTokens)
    ? r.inputTokens + r.outputTokens : null;
  const prefixes = (rows: Usage[]) => {
    const tokens = [0], missing = [0];
    for (const row of rows) {
      const value = amount(row);
      tokens.push(tokens[tokens.length - 1] + (value ?? 0));
      missing.push(missing[missing.length - 1] + (value === null ? 1 : 0));
    }
    return { tokens, missing };
  };
  const ended = prefixes(complete), begun = prefixes(startsOrdered);
  const unknownEnds = requests.filter((r) => r.completedAt !== null &&
    (r.startedAt === null || r.startedAt > r.completedAt)).map((r) => r.completedAt).sort((a, b) => a - b);
  const unknownStarts = requests.filter((r) => r.completedAt === null).map((r) => r.startedAt ?? -Infinity).sort((a, b) => a - b);
  const boundary = (at: number) => {
    const started = bound(starts, at, true), finished = bound(ends, at, false);
    return { tokens: begun.tokens[started] - ended.tokens[finished], missing: begun.missing[started] - ended.missing[finished] };
  };
  return (left: number, right: number) => {
    const lo = bound(ends, left, true), hi = bound(ends, right, false);
    const l = boundary(left), r = boundary(right);
    const tokens = ended.tokens[hi] - ended.tokens[lo];
    return {
      tokens, count: hi - lo, low: Math.max(0, tokens - l.tokens), high: tokens + r.tokens,
      allowance: l.tokens + r.tokens,
      missing: ended.missing[hi] !== ended.missing[lo] || l.missing > 0 || r.missing > 0 ||
        bound(unknownEnds, left, false) < unknownEnds.length || bound(unknownStarts, right, true) > 0,
    };
  };
}

/** Estimate token-equivalent quota only from disjoint, reset-compatible evidence. */
export function estimateQuotaCapacity(input: {
  windowKey: string;
  observations: Observation[];
  requests: Usage[];
  now: number;
  unavailableReason?: CapacityReason;
}): CapacityEstimate {
  const { windowKey, now } = input;
  const result = makeUnknown(windowKey, "no_observations");
  if (input.unavailableReason) return makeUnknown(windowKey, input.unavailableReason);
  const history = input.observations.filter((o) => o.observedAt >= now - 30 * DAY && o.observedAt <= now);
  const current = [...input.observations].sort((a, b) => b.observedAt - a.observedAt)[0];
  if (!current) return result;
  if (!Number.isFinite(current.observedAt)) return { ...result, reason: "inconsistent_observations" };
  result.currentObservedAt = new Date(current.observedAt).toISOString();
  result.currentUsedRatio = validRatio(current.usedRatio) ? current.usedRatio : null;
  if (current.resetsAt !== null && Number.isFinite(current.resetsAt)) {
    result.resetsAt = new Date(current.resetsAt).toISOString();
  }
  if (current.observedAt > now) return { ...result, reason: "current_future" };
  if (now - current.observedAt > 300_000) return { ...result, reason: "current_stale" };
  if (current.resetsAt === null || !Number.isFinite(current.resetsAt)) return { ...result, reason: "missing_reset" };
  if (current.resetsAt <= now) return { ...result, reason: "reset_elapsed" };
  if (!validRatio(current.usedRatio)) return { ...result, reason: "inconsistent_observations" };

  const byTime = new Map<number, Observation>();
  for (const sample of [...history, current]) {
    if (sample.resetsAt !== current.resetsAt || sample.observedAt > current.observedAt) continue;
    if (!validRatio(sample.usedRatio) || sample.observedAt >= current.resetsAt) {
      return { ...result, reason: "inconsistent_observations" };
    }
    const prior = byTime.get(sample.observedAt);
    if (prior && prior.usedRatio !== sample.usedRatio) {
      return { ...result, reason: "inconsistent_observations" };
    }
    byTime.set(sample.observedAt, sample);
  }
  const samples = [...byTime.entries()].sort((a, b) => a[0] - b[0]);
  result.observationCount = samples.length;
  result.status = "insufficient";
  result.reason = "insufficient_delta";
  if (samples.length < 2) return result;
  for (let i = 1; i < samples.length; i++) {
    if ((samples[i][1].usedRatio as number) < (samples[i - 1][1].usedRatio as number)) {
      return { ...result, reason: "inconsistent_observations" };
    }
  }
  const requests = input.requests.filter((request) =>
    request.completedAt !== null
      ? request.completedAt <= now
      : request.startedAt === null || request.startedAt <= now
  );
  const intervalStats = indexUsage(requests);

  const accepted: Array<{ capacity: number; delta: number; tokens: number; low: number; high: number; allowance: number; count: number; start: number; end: number; lowerBound: boolean }> = [];
  let anchor = samples[0];
  let rejection: CapacityReason | null = null;
  let pendingBoundary = false;
  for (const sample of samples.slice(1)) {
    const delta = (sample[1].usedRatio as number) - (anchor[1].usedRatio as number);
    if (delta + 1e-12 < 0.05) continue;
    const left = anchor[0];
    const right = sample[0];
    const { tokens, count, low, high, allowance, missing } = intervalStats(left, right);
    if (missing) {
      rejection = "incomplete_usage";
      if (low > 0) accepted.push({ capacity: low / delta, delta, tokens, low, high: 0, allowance, count, start: left, end: right, lowerBound: true });
    } else if (allowance > tokens * 0.1) {
      rejection = "boundary_overlap";
      pendingBoundary = true;
      continue;
    } else if (tokens > 0) {
      accepted.push({ capacity: tokens / delta, delta, tokens, low, high, allowance, count, start: left, end: right, lowerBound: false });
    } else {
      rejection = "no_matched_usage";
    }
    anchor = sample;
    pendingBoundary = false;
  }

  const complete = accepted.filter((item) => !item.lowerBound);
  const chosen = complete.length ? complete : accepted;
  if (!chosen.length) return { ...result, reason: pendingBoundary ? "boundary_overlap" : rejection ?? "insufficient_delta" };
  const weighted = [...chosen].sort((a, b) => a.capacity - b.capacity);
  const midpoint = chosen.reduce((sum, item) => sum + item.delta, 0) / 2;
  let acc = 0;
  let capacity = weighted[weighted.length - 1].capacity;
  for (const item of weighted) {
    acc += item.delta;
    if (acc + 1e-12 >= midpoint) { capacity = item.capacity; break; }
  }
  const lowerBound = complete.length === 0;
  return {
    ...result,
    status: "estimated",
    reason: lowerBound ? "incomplete_usage" : "valid",
    estimateKind: lowerBound ? "recorded_lower_bound" : "empirical_estimate",
    estimatedTotalTokens: capacity,
    remainingTokens: capacity * (1 - (current.usedRatio as number)),
    totalTokensLow: Math.min(...chosen.map((item) => item.low / item.delta)),
    totalTokensHigh: lowerBound ? null : Math.max(...complete.map((item) => item.high / item.delta)),
    observedTokens: chosen.reduce((sum, item) => sum + item.tokens, 0),
    boundaryUncertaintyTokens: chosen.reduce((sum, item) => sum + item.allowance, 0),
    sampleCount: chosen.length,
    requestCount: chosen.reduce((sum, item) => sum + item.count, 0),
    usedRatioDelta: chosen.reduce((sum, item) => sum + item.delta, 0),
    evidenceStart: new Date(Math.min(...chosen.map((item) => item.start))).toISOString(),
    evidenceEnd: new Date(Math.max(...chosen.map((item) => item.end))).toISOString(),
    caveats: lowerBound ? [...CAVEATS, "Recorded minimum equivalent only: unknown usage was not counted as zero; total allocation and upper range remain unknown."] : CAVEATS,
  };
}

export function getConnectionQuotaCapacity(connectionId: string, now = Date.now()) {
  const evidence = getQuotaCapacityEvidence(connectionId, new Date(now - 30 * DAY).toISOString(), new Date(now).toISOString());
  if (!evidence.connection) return null;
  const { provider } = evidence.connection;
  const groups = new Map<string, Observation[]>();
  const unsupportedScopes = new Set<string>();
  const identityCutoffs = new Map<string, number>();
  for (const row of evidence.snapshots) {
    if (!row.identityMatches) {
      const observedAt = Date.parse(row.createdAt);
      identityCutoffs.set(row.windowKey, Math.max(identityCutoffs.get(row.windowKey) ?? -Infinity,
        Number.isFinite(observedAt) ? observedAt : Infinity));
    }
  }
  for (const row of evidence.snapshots) {
    const list = groups.get(row.windowKey) ?? [];
    if (row.identityMatches && Date.parse(row.createdAt) > (identityCutoffs.get(row.windowKey) ?? -Infinity)) {
      list.push({ observedAt: Date.parse(row.createdAt), usedRatio: row.remainingPercentage === null ? null : 1 - row.remainingPercentage / 100, resetsAt: row.resetsAt ? Date.parse(row.resetsAt) : null });
    }
    groups.set(row.windowKey, list);
    if (row.unsupportedScope) unsupportedScopes.add(row.windowKey);
  }
  const current = getProviderLimitsCache(connectionId);
  if (current) {
    for (const [windowKey, observations] of groups) {
      if (!(Date.parse(current.fetchedAt) > (identityCutoffs.get(windowKey) ?? -Infinity))) continue;
      const raw = current.quotas?.[windowKey];
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
      const quota = raw as Record<string, unknown>;
      const percent = quota.remainingPercentage;
      observations.push({
        observedAt: Date.parse(current.fetchedAt),
        usedRatio: quota.fractionReported !== false && typeof percent === "number" &&
          Number.isFinite(percent) && percent >= 0 && percent <= 100 ? 1 - percent / 100 : null,
        resetsAt: typeof quota.resetAt === "string" ? Date.parse(quota.resetAt) : null,
      });
    }
  }
  const requests: Usage[] = evidence.requests.map((row) => {
    const startedAt = Date.parse(row.startedAt);
    const duration = Number.isFinite(row.durationMs) && row.durationMs > 0 ? row.durationMs : null;
    const completedAt = Number.isFinite(startedAt) && duration !== null ? startedAt + duration : null;
    return { startedAt: Number.isFinite(startedAt) ? startedAt : null, completedAt, inputTokens: row.inputTokens, outputTokens: row.outputTokens, success: row.success, reported: row.reported };
  });
  return {
    provider,
    connectionId,
    windows: [...groups.entries()].map(([windowKey, observations]) =>
      estimateQuotaCapacity({
        windowKey,
        observations,
        requests: requests.map((request) => request.startedAt !== null &&
          request.startedAt <= (identityCutoffs.get(windowKey) ?? -Infinity)
          ? { ...request, inputTokens: null, outputTokens: null, reported: false } : request),
        now,
        unavailableReason: !evidence.connection.identity ? "identity_ambiguous" : unsupportedScopes.has(windowKey) ? "unsupported_scope" : undefined,
      })
    ),
  };
}
