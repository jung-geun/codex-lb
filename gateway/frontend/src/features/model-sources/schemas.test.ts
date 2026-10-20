import { describe, expect, it } from "vitest";

import {
  ModelSourceCreateRequestSchema,
  ModelSourceQuotaHistorySchema,
  ModelSourceSchema,
  ModelSourcesResponseSchema,
  ModelSourceUpdateRequestSchema,
  SourcePresetsSchema,
  SourceOAuthStatusSchema,
} from "@/features/model-sources/schemas";

const ISO = "2026-01-01T00:00:00+00:00";

const BASE_SOURCE = {
  id: "src_vllm",
      name: "vLLM",
      kind: "openai_compatible",
      baseUrl: "http://localhost:8000/v1",
      isEnabled: true,
      healthStatus: "unknown",
      supportsChatCompletions: true,
      supportsResponses: false,
      supportsAudioTranscriptions: true,
      supportsEmbeddings: true,
      timeoutSeconds: null,
      maxConcurrency: null,
      createdAt: ISO,
      updatedAt: ISO,
      models: [
        {
          id: 1,
          sourceId: "src_vllm",
          model: "local-coder",
          displayName: "Local Coder",
          contextWindow: 8192,
          maxOutputTokens: 1024,
          supportsStreaming: true,
          supportsTools: true,
          supportsVision: false,
          inputPer1M: null,
          cachedInputPer1M: null,
          outputPer1M: null,
          rawMetadataJson: null,
          isEnabled: true,
          createdAt: ISO,
          updatedAt: ISO,
        },
  ],
};

describe("ModelSourceSchema", () => {
  it("parses model source payload", () => {
    const parsed = ModelSourceSchema.parse(BASE_SOURCE);

    expect(parsed.id).toBe("src_vllm");
    expect(parsed.supportsAudioTranscriptions).toBe(true);
    expect(parsed.supportsEmbeddings).toBe(true);
    expect(parsed.models[0].model).toBe("local-coder");
  });

  it("defaults supportsEmbeddings to false when the field is absent", () => {
    const withoutEmbeddings: Record<string, unknown> = { ...BASE_SOURCE };
    delete withoutEmbeddings.supportsEmbeddings;

    const parsed = ModelSourceSchema.parse(withoutEmbeddings);

    expect(parsed.supportsEmbeddings).toBe(false);
  });

  it("preserves safe entitlement and quota observations without exposing OAuth secrets", () => {
    const parsed = ModelSourceSchema.parse({
      ...BASE_SOURCE,
      provider: "claude",
      authType: "claude_oauth",
      billingMode: "subscription",
      entitlementStatus: "reauth_required",
      supportsMessages: true,
      priority: 3,
      quotaGroup: "same-plan",
      quotaError: "Refresh failed",
      accessToken: "must-not-be-retained",
      refreshToken: "must-not-be-retained",
      quotaWindows: [{ windowKey: "seven_day", model: null, usedRatio: 1, limitValue: null, usedValue: null, resetsAt: ISO, observedAt: ISO }],
    });
    expect(parsed).toMatchObject({ provider: "claude", entitlementStatus: "reauth_required", supportsMessages: true, priority: 3, quotaGroup: "same-plan" });
    expect(parsed.quotaWindows?.[0].usedRatio).toBe(1);
    expect(parsed).not.toHaveProperty("accessToken");
    expect(parsed).not.toHaveProperty("refreshToken");
  });

  it("keeps safe account metadata nullable and optional, without inventing credential presence", () => {
    const absent = ModelSourceSchema.parse(BASE_SOURCE);
    expect(absent.subscriptionType).toBeUndefined();
    expect(absent.accountUuid).toBeUndefined();
    expect(absent.organizationUuid).toBeUndefined();
    expect(absent.hasAccessToken).toBeUndefined();
    expect(absent.hasRefreshToken).toBeUndefined();

    const metadata = {
      subscriptionType: "max", accountUuid: "account-1", organizationUuid: "org-1",
      hasAccessToken: true, hasRefreshToken: false,
    };
    expect(ModelSourceSchema.parse({ ...BASE_SOURCE, ...metadata })).toMatchObject(metadata);
    expect(ModelSourceSchema.parse({
      ...BASE_SOURCE, subscriptionType: null, accountUuid: null, organizationUuid: null,
    })).toMatchObject({ subscriptionType: null, accountUuid: null, organizationUuid: null });
    expect(ModelSourceSchema.safeParse({ ...BASE_SOURCE, hasAccessToken: null }).success).toBe(false);
  });
});

describe("ModelSourcesResponseSchema", () => {
  it("requires an explicit source list instead of hiding malformed reads as zero accounts", () => {
    expect(ModelSourcesResponseSchema.safeParse({}).success).toBe(false);
    expect(ModelSourcesResponseSchema.parse({ sources: [] }).sources).toEqual([]);
  });
});

describe("ModelSourceQuotaHistorySchema", () => {
  const interval = {
    sourceId: "src_claude", windowStart: "2026-01-01T00:00:00Z", windowEnd: "2026-01-08T00:00:00Z",
  };

  it("accepts explicitly empty history, but does not turn an absent history array into observations", () => {
    expect(ModelSourceQuotaHistorySchema.parse({ ...interval, windows: [] }).windows).toEqual([]);
    expect(ModelSourceQuotaHistorySchema.safeParse(interval).success).toBe(false);
    expect(ModelSourceQuotaHistorySchema.safeParse({ ...interval, windows: null }).success).toBe(false);
  });

  it("retains unknown ratios, unknown window names, and distinct model/window observations", () => {
    const observation = { usedRatio: null, limitValue: null, usedValue: null, resetsAt: null, observedAt: ISO };
    const windows = [
      { ...observation, windowKey: "five_hour", model: null },
      { ...observation, windowKey: "seven_day", model: "claude-sonnet-4-6", usedRatio: 0 },
      { ...observation, windowKey: "seven_day", model: "claude-opus-4-6", usedRatio: 1 },
      { ...observation, windowKey: "future_window", model: "claude-sonnet-4-6" },
    ];
    expect(ModelSourceQuotaHistorySchema.parse({ ...interval, windows }).windows).toEqual(windows);
  });

  it("requires actual observation timestamps and does not coerce missing percentages to zero", () => {
    const window = { windowKey: "five_hour", model: null, usedRatio: null, limitValue: null, usedValue: null, resetsAt: null };
    expect(ModelSourceQuotaHistorySchema.safeParse({ ...interval, windows: [window] }).success).toBe(false);
    expect(ModelSourceQuotaHistorySchema.safeParse({ ...interval, windows: [{ ...window, observedAt: "invalid" }] }).success).toBe(false);
    expect(ModelSourceQuotaHistorySchema.safeParse({
      ...interval, windows: [{ ...window, observedAt: ISO, usedRatio: "0" }],
    }).success).toBe(false);
  });
});

describe("ModelSourceCreateRequestSchema", () => {
  it("accepts OpenAI-compatible source creation payload", () => {
    const parsed = ModelSourceCreateRequestSchema.parse({
      name: "DeepSeek",
      baseUrl: "https://api.deepseek.com/v1",
      apiKey: "secret",
      supportsChatCompletions: true,
      supportsResponses: true,
      supportsAudioTranscriptions: true,
      supportsEmbeddings: true,
      models: [{ model: "deepseek-v4-flash" }],
    });

    expect(parsed.supportsAudioTranscriptions).toBe(true);
    expect(parsed.supportsEmbeddings).toBe(true);
    expect(parsed.models[0].model).toBe("deepseek-v4-flash");
  });

  it("sends routing and Messages fields but cannot claim a verified entitlement", () => {
    const parsed = ModelSourceCreateRequestSchema.parse({
      name: "Claude", baseUrl: "https://api.anthropic.com", provider: "claude",
      authType: "claude_oauth", billingMode: "subscription", kind: "anthropic_compatible",
      supportsMessages: true, supportsCountTokens: true, priority: 7, quotaGroup: "team-plan",
      entitlementStatus: "verified", extraUsageEnabled: false, models: [{ model: "claude-sonnet-4-6" }],
    });
    expect(parsed).toMatchObject({ provider: "claude", supportsMessages: true, supportsCountTokens: true, priority: 7, quotaGroup: "team-plan" });
    expect(parsed).not.toHaveProperty("entitlementStatus");
    expect(parsed).not.toHaveProperty("extraUsageEnabled");
    expect(parsed).not.toHaveProperty("kind");
  });
});

describe("ModelSourceUpdateRequestSchema", () => {
  it("accepts disabling a source", () => {
    const parsed = ModelSourceUpdateRequestSchema.parse({
      isEnabled: false,
    });

    expect(parsed.isEnabled).toBe(false);
  });
});

describe("provider connection DTOs", () => {
  it("parses real preset arrays without inventing account model capabilities", () => {
    const presets = SourcePresetsSchema.parse([{
      provider: "claude", name: "Claude", baseUrl: "https://api.anthropic.com",
      authType: "claude_oauth", billingMode: "subscription", supportsMessages: true,
      supportsCountTokens: true, models: [], connectionNote: "Verify entitlement after authorization",
    }]);
    expect(presets[0].models).toEqual([]);
    expect(presets[0].supportsMessages).toBe(true);
  });

  it.each(["openai", "anthropic"] as const)("preserves %s identity and explicit API capabilities", (provider) => {
    const [preset] = SourcePresetsSchema.parse([{
      provider, name: provider, baseUrl: "https://provider.example/v1", authType: "api_key", billingMode: "api_metered",
      supportsMessages: provider === "anthropic", supportsChatCompletions: provider === "openai",
      supportsResponses: provider === "openai", supportsAudioTranscriptions: provider === "openai", supportsEmbeddings: provider === "openai",
      connectionNote: "API key", models: [],
    }]);
    expect(preset.provider).toBe(provider);
    expect(preset.supportsChatCompletions).toBe(provider === "openai");
    expect(preset.supportsResponses).toBe(provider === "openai");
    expect(preset.supportsEmbeddings).toBe(provider === "openai");
    expect(preset.supportsAudioTranscriptions).toBe(provider === "openai");
    expect(ModelSourceSchema.parse({ ...BASE_SOURCE, provider, authType: "api_key", billingMode: "api_metered" }).provider).toBe(provider);
  });

  it("preserves terminal authorization errors without any credential fields", () => {
    const status = SourceOAuthStatusSchema.parse({ flowId: "flow", status: "failed", expiresAt: ISO, error: "Authorization expired", accessToken: "not-retained" });
    expect(status.error).toBe("Authorization expired");
    expect(status).not.toHaveProperty("accessToken");
  });
});
