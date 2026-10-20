import { z } from "zod";

const timestamp = z.iso.datetime({ offset: true }).nullable();
const tokens = z.number().finite().nonnegative().nullable();

export const QuotaEstimateSchema = z.object({
  windowKey: z.string(),
  model: z.string().nullable(),
  status: z.enum(["estimated", "insufficient", "unknown"]),
  reason: z.enum([
    "valid", "no_observations", "unsupported_scope", "identity_ambiguous", "missing_reset",
    "current_stale", "current_future", "reset_elapsed", "insufficient_delta", "incomplete_usage",
    "boundary_overlap", "inconsistent_observations", "no_matched_usage",
  ]),
  estimatedTotalTokens: tokens,
  remainingTokens: tokens,
  observedTokens: z.number().int().nonnegative(),
  boundaryUncertaintyTokens: z.number().int().nonnegative().default(0),
  sampleCount: z.number().int().nonnegative(),
  observationCount: z.number().int().nonnegative(),
  requestCount: z.number().int().nonnegative(),
  usedRatioDelta: z.number().finite().nonnegative(),
  evidenceStart: timestamp,
  evidenceEnd: timestamp,
  currentObservedAt: timestamp,
  resetsAt: timestamp,
  currentUsedRatio: z.number().min(0).max(1).nullable(),
  basis: z.literal("observed_gateway_input_plus_output_tokens"),
  estimateKind: z.enum(["empirical_estimate", "recorded_lower_bound"]).nullable(),
  totalTokensLow: tokens,
  totalTokensHigh: tokens,
  caveats: z.array(z.string()),
});

export const QuotaEstimatesResponseSchema = z.object({
  accounts: z.array(z.object({ accountId: z.string(), sharedAccountIds: z.array(z.string()).default([]), windows: z.array(QuotaEstimateSchema) })),
  sources: z.array(z.object({ sourceId: z.string(), sharedSourceIds: z.array(z.string()).default([]), windows: z.array(QuotaEstimateSchema) })),
});

export type QuotaEstimate = z.infer<typeof QuotaEstimateSchema>;
export type QuotaEstimatesResponse = z.infer<typeof QuotaEstimatesResponseSchema>;
