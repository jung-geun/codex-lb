import { z } from "zod";

export const SourceProviderSchema = z.enum(["custom", "openai", "anthropic", "claude", "zai", "kimi", "nanogpt", "deepseek"]);
export const SourceAuthTypeSchema = z.enum(["none", "api_key", "claude_oauth"]);
export const SourceBillingModeSchema = z.enum(["subscription", "coding_plan", "api_metered", "unknown"]);
export const SourceKeyTypeSchema = z.enum(["personal", "team", "compatible", "standard", "unknown"]);

const SourceWarmupSchema = z.object({
  status: z.string(),
  window: z.string(),
  resetAt: z.iso.datetime({ offset: true }).nullable(),
  model: z.string().nullable(),
  attemptedAt: z.iso.datetime({ offset: true }),
  completedAt: z.iso.datetime({ offset: true }).nullable(),
  errorMessage: z.string().nullable(),
});

const sourceConnectionFields = {
  provider: SourceProviderSchema.optional(),
  authType: SourceAuthTypeSchema.optional(),
  billingMode: SourceBillingModeSchema.optional(),
  keyType: SourceKeyTypeSchema.optional(),
  supportsMessages: z.boolean().optional(),
  supportsCountTokens: z.boolean().optional(),
  messagesBaseUrl: z.string().max(2048).nullable().optional(),
  priority: z.number().int().optional(),
  quotaGroup: z.string().min(1).max(128).regex(/^[A-Za-z0-9][A-Za-z0-9:_.-]*$/).nullable().optional(),
};

export const SourceQuotaWindowSchema = z.object({
  windowKey: z.string(),
  model: z.string().nullable(),
  usedRatio: z.number().min(0).max(1).nullable(),
  limitValue: z.number().nullable(),
  usedValue: z.number().nullable(),
  resetsAt: z.iso.datetime({ offset: true }).nullable(),
  observedAt: z.iso.datetime({ offset: true }),
});

export type SourceQuotaWindow = z.infer<typeof SourceQuotaWindowSchema>;

export const ModelSourceQuotaHistorySchema = z.object({
  sourceId: z.string(),
  windowStart: z.iso.datetime({ offset: true }),
  windowEnd: z.iso.datetime({ offset: true }),
  windows: z.array(SourceQuotaWindowSchema),
});
export type ModelSourceQuotaHistoryResponse = z.infer<typeof ModelSourceQuotaHistorySchema>;

export const ModelSourceModelSchema = z.object({
  id: z.number(),
  sourceId: z.string(),
  model: z.string(),
  displayName: z.string().nullable().default(null),
  contextWindow: z.number().int().positive().nullable().default(null),
  maxOutputTokens: z.number().int().positive().nullable().default(null),
  supportsStreaming: z.boolean().default(true),
  supportsTools: z.boolean().default(false),
  supportsVision: z.boolean().default(false),
  inputPer1M: z.number().nullable().default(null),
  cachedInputPer1M: z.number().nullable().default(null),
  outputPer1M: z.number().nullable().default(null),
  audioPerMinute: z.number().nullable().default(null),
  rawMetadataJson: z.string().nullable().default(null),
  upstreamModel: z.string().nullable().optional(),
  subscriptionEligible: z.boolean().optional(),
  subscriptionRestricted: z.boolean().optional(),
  cacheCreationPer1M: z.number().nullable().optional(),
  cacheCreation1HPer1M: z.number().nullable().optional(),
  isEnabled: z.boolean().default(true),
  createdAt: z.iso.datetime({ offset: true }),
  updatedAt: z.iso.datetime({ offset: true }),
});

export const ModelSourceSchema = z.object({
  id: z.string(),
  name: z.string(),
  kind: z.string(),
  provider: SourceProviderSchema.optional(),
  authType: SourceAuthTypeSchema.optional(),
  billingMode: SourceBillingModeSchema.optional(),
  keyType: SourceKeyTypeSchema.optional(),
  detectedKeyType: SourceKeyTypeSchema.optional(),
  entitlementStatus: z.enum(["unverified", "verified", "reauth_required", "blocked"]).optional(),
  entitlementVerifiedAt: z.iso.datetime({ offset: true }).nullable().optional(),
  entitlementDetail: z.string().nullable().optional(),
  extraUsageEnabled: z.boolean().nullable().optional(),
  credentialExpiresAt: z.iso.datetime({ offset: true }).nullable().optional(),
  accountLabel: z.string().nullable().optional(),
  subscriptionType: z.string().nullable().optional(),
  accountUuid: z.string().nullable().optional(),
  organizationUuid: z.string().nullable().optional(),
  hasAccessToken: z.boolean().optional(),
  hasRefreshToken: z.boolean().optional(),
  priority: z.number().int().optional(),
  quotaGroup: z.string().nullable().optional(),
  routingPolicy: z.enum(["normal", "burn_first", "preserve"]).default("normal"),
  apiFallbackOnly: z.boolean().default(true),
  limitWarmupEnabled: z.boolean().default(false),
  warmupSupported: z.boolean().default(false),
  warmupUnavailableReason: z.string().nullable().default(null),
  limitWarmup: SourceWarmupSchema.nullable().default(null),
  cooldownUntil: z.iso.datetime({ offset: true }).nullable().optional(),
  quotaCheckedAt: z.iso.datetime({ offset: true }).nullable().optional(),
  quotaError: z.string().nullable().optional(),
  quotaWindows: z.array(SourceQuotaWindowSchema).optional(),
  supportsMessages: z.boolean().optional(),
  supportsCountTokens: z.boolean().optional(),
  messagesBaseUrl: z.string().nullable().optional(),
  baseUrl: z.string(),
  isEnabled: z.boolean(),
  healthStatus: z.string(),
  supportsChatCompletions: z.boolean(),
  supportsResponses: z.boolean(),
  supportsAudioTranscriptions: z.boolean().default(false),
  supportsEmbeddings: z.boolean().default(false),
  timeoutSeconds: z.number().int().positive().nullable().default(null),
  maxConcurrency: z.number().int().positive().nullable().default(null),
  createdAt: z.iso.datetime({ offset: true }),
  updatedAt: z.iso.datetime({ offset: true }),
  models: z.array(ModelSourceModelSchema).default([]),
});

export const ModelSourcesResponseSchema = z.object({
  sources: z.array(ModelSourceSchema),
});
export type ModelSourcesResponse = z.infer<typeof ModelSourcesResponseSchema>;

export const ModelSourceUsageSummarySchema = z.object({
  sourceId: z.string(),
  windowStart: z.iso.datetime({ offset: true }),
  windowEnd: z.iso.datetime({ offset: true }),
  requestCount: z.number().int().nonnegative(),
  successCount: z.number().int().nonnegative(),
  errorCount: z.number().int().nonnegative(),
  cancelledCount: z.number().int().nonnegative(),
  inputTokens: z.number().int().nonnegative().nullable(),
  outputTokens: z.number().int().nonnegative().nullable(),
  cachedInputTokens: z.number().int().nonnegative().nullable(),
  cacheCreationInputTokens: z.number().int().nonnegative().nullable(),
  reasoningTokens: z.number().int().nonnegative().nullable(),
  costUsd: z.number().nullable(),
  knownCostUsd: z.number().nullable(),
  unpricedRequestCount: z.number().int().nonnegative(),
  unknownUsageCount: z.number().int().nonnegative(),
  lastRequestAt: z.iso.datetime({ offset: true }).nullable(),
});
export type ModelSourceUsageSummary = z.infer<typeof ModelSourceUsageSummarySchema>;

export const ModelSourceApiUsageSchema = z.object({
  sourceId: z.string(),
  observedAt: z.iso.datetime({ offset: true }).nullable(),
  rateLimits: z.array(z.object({
    model: z.string().nullable(),
    tokenKind: z.enum(["tokens", "input_tokens", "output_tokens", "project_tokens"]).default("tokens"),
    observedAt: z.iso.datetime({ offset: true }),
    tokenLimit: z.number().nonnegative().nullable(),
    tokensRemaining: z.number().nonnegative().nullable(),
    tokensResetAt: z.iso.datetime({ offset: true }).nullable(),
    requestLimit: z.number().nonnegative().nullable(),
    requestsRemaining: z.number().nonnegative().nullable(),
    requestsResetAt: z.iso.datetime({ offset: true }).nullable(),
  })),
  balances: z.array(z.object({
    currency: z.string(),
    totalBalance: z.string(),
    grantedBalance: z.string().nullable(),
    toppedUpBalance: z.string().nullable(),
  })),
  balanceObservedAt: z.iso.datetime({ offset: true }).nullable(),
  balanceStatus: z.enum(["available", "unavailable", "unknown"]),
  balanceError: z.string().nullable(),
  balanceIsAvailable: z.boolean().nullable(),
});
export type ModelSourceApiUsage = z.infer<typeof ModelSourceApiUsageSchema>;

export const ModelSourceModelInputSchema = z.object({
  model: z.string().min(1).max(255),
  displayName: z.string().max(255).nullable().optional(),
  contextWindow: z.number().int().positive().nullable().optional(),
  maxOutputTokens: z.number().int().positive().nullable().optional(),
  supportsStreaming: z.boolean().optional(),
  supportsTools: z.boolean().optional(),
  supportsVision: z.boolean().optional(),
  inputPer1M: z.number().nonnegative().nullable().optional(),
  cachedInputPer1M: z.number().nonnegative().nullable().optional(),
  outputPer1M: z.number().nonnegative().nullable().optional(),
  audioPerMinute: z.number().nonnegative().nullable().optional(),
  rawMetadataJson: z.string().nullable().optional(),
  upstreamModel: z.string().max(255).nullable().optional(),
  subscriptionEligible: z.boolean().optional(),
  cacheCreationPer1M: z.number().nonnegative().nullable().optional(),
  cacheCreation1HPer1M: z.number().nonnegative().nullable().optional(),
  isEnabled: z.boolean().optional(),
});

export const ModelSourceCreateRequestSchema = z.object({
  ...sourceConnectionFields,
  apiFallbackOnly: z.boolean().default(true),
  name: z.string().min(1).max(128),
  baseUrl: z.string().min(1).max(2048),
  apiKey: z.string().min(1).nullable().optional(),
  supportsChatCompletions: z.boolean().optional(),
  supportsResponses: z.boolean().optional(),
  supportsAudioTranscriptions: z.boolean().optional(),
  supportsEmbeddings: z.boolean().optional(),
  timeoutSeconds: z.number().int().positive().nullable().optional(),
  maxConcurrency: z.number().int().positive().nullable().optional(),
  models: z.array(ModelSourceModelInputSchema).default([]),
});

export const ModelSourceUpdateRequestSchema = z.object({
  ...sourceConnectionFields,
  name: z.string().min(1).max(128).optional(),
  baseUrl: z.string().min(1).max(2048).optional(),
  apiKey: z.string().min(1).nullable().optional(),
  isEnabled: z.boolean().optional(),
  routingPolicy: z.enum(["normal", "burn_first", "preserve"]).optional(),
  apiFallbackOnly: z.boolean().optional(),
  limitWarmupEnabled: z.boolean().optional(),
  supportsChatCompletions: z.boolean().optional(),
  supportsResponses: z.boolean().optional(),
  supportsAudioTranscriptions: z.boolean().optional(),
  supportsEmbeddings: z.boolean().optional(),
  timeoutSeconds: z.number().int().positive().nullable().optional(),
  maxConcurrency: z.number().int().positive().nullable().optional(),
  models: z.array(ModelSourceModelInputSchema).optional(),
});

export type ModelSource = z.infer<typeof ModelSourceSchema>;
export type ModelSourceModel = z.infer<typeof ModelSourceModelSchema>;
export type ModelSourceModelInput = z.infer<typeof ModelSourceModelInputSchema>;
export type ModelSourceCreateRequest = z.input<typeof ModelSourceCreateRequestSchema>;
export type ModelSourceUpdateRequest = z.infer<typeof ModelSourceUpdateRequestSchema>;

export const SourceOAuthStartSchema = z.object({
  flowId: z.string(),
  authorizationUrl: z.url().refine((value) => new URL(value).protocol === "https:"),
  expiresAt: z.iso.datetime({ offset: true }),
});

export const SourceOAuthStatusSchema = z.object({
  flowId: z.string(),
  status: z.enum(["pending", "exchanging", "completed", "cancelled", "expired", "failed"]),
  expiresAt: z.iso.datetime({ offset: true }),
  error: z.string().nullable().optional(),
});

export type SourceOAuthFlow = z.infer<typeof SourceOAuthStartSchema>;

export const SourcePresetSchema = z.object({
  name: z.string(),
  provider: SourceProviderSchema,
  authType: SourceAuthTypeSchema,
  billingMode: SourceBillingModeSchema,
  baseUrl: z.string(),
  messagesBaseUrl: z.string().nullable().optional(),
  supportsMessages: z.boolean().default(false),
  supportsCountTokens: z.boolean().default(false),
  supportsChatCompletions: z.boolean().default(false),
  supportsResponses: z.boolean().default(false),
  supportsAudioTranscriptions: z.boolean().default(false),
  supportsEmbeddings: z.boolean().default(false),
  models: z.array(z.string()).default([]),
  connectionNote: z.string(),
});
export const SourcePresetsSchema = z.array(SourcePresetSchema);
export type SourcePreset = z.infer<typeof SourcePresetSchema>;
