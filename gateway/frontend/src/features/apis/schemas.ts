import { z } from "zod";

const ApiKeyTrendPointSchema = z.object({
  t: z.iso.datetime({ offset: true }),
  v: z.number(),
});

export const ApiKeyTrendsResponseSchema = z.object({
  keyId: z.string(),
  cost: z.array(ApiKeyTrendPointSchema),
  tokens: z.array(ApiKeyTrendPointSchema),
});

const ApiKeyAccountCostSchema = z.object({
  accountId: z.string().nullable().default(null),
  email: z.string().nullable().default(null),
  costUsd: z.number().default(0),
  isDeleted: z.boolean().default(false),
  modelSourceId: z.string().nullable().optional(),
  sourceName: z.string().nullable().optional(),
  provider: z.string().nullable().optional(),
  billingMode: z.string().nullable().optional(),
  requestCount: z.number().int().nonnegative().optional(),
  unpricedRequestCount: z.number().int().nonnegative().optional(),
});

export const ApiKeyUsage7DayResponseSchema = z.object({
  keyId: z.string(),
  totalTokens: z.number().int(),
  totalCostUsd: z.number(),
  totalRequests: z.number().int(),
  cachedInputTokens: z.number().int(),
  accountCosts: z.array(ApiKeyAccountCostSchema).default([]),
});

export type ApiKeyAccountCost = z.infer<typeof ApiKeyAccountCostSchema>;
export type ApiKeyTrendPoint = z.infer<typeof ApiKeyTrendPointSchema>;
export type ApiKeyTrendsResponse = z.infer<typeof ApiKeyTrendsResponseSchema>;
export type ApiKeyUsage7DayResponse = z.infer<typeof ApiKeyUsage7DayResponseSchema>;
