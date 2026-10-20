import type { ModelSource } from "@/features/model-sources/schemas";
import { sourceWindowIsStale } from "@/features/model-sources/monitoring";
import type { AccountSummary } from "@/features/accounts/schemas";
import type { QuotaEstimate } from "@/features/quota-estimates/schemas";

/** Display-only projection; source ids must never be dispatched to Codex account mutations. */
export type AccountView = Omit<AccountSummary, "limitWarmup"> & {
  source?: ModelSource;
  limitWarmup?: AccountSummary["limitWarmup"] | ModelSource["limitWarmup"];
  quotaEstimates?: QuotaEstimate[];
  quotaEstimateState?: "loading" | "error" | "ready";
  quotaSharedCount?: number;
};

export function sourceToAccountView(source: ModelSource): AccountView {
  const primary = source.quotaWindows?.find((window) => window.windowKey === "five_hour" && window.model === null);
  const secondary = source.quotaWindows?.find((window) => window.windowKey === "seven_day" && window.model === null);
  const identity = source.accountLabel?.trim() || source.name;
  return {
    accountId: sourceSelectionId(source.id),
    email: identity,
    displayName: identity,
    alias: identity !== source.name ? source.name : null,
    planType: source.subscriptionType ?? source.billingMode ?? "unknown",
    status: sourceStatus(source),
    usage: {
      primaryRemainingPercent: primary?.usedRatio == null ? null : (1 - primary.usedRatio) * 100,
      secondaryRemainingPercent: secondary?.usedRatio == null ? null : (1 - secondary.usedRatio) * 100,
    },
    windowMinutesPrimary: primary || source.authType === "claude_oauth" ? 300 : null,
    windowMinutesSecondary: secondary || source.authType === "claude_oauth" ? 10_080 : null,
    resetAtPrimary: primary?.resetsAt ?? null,
    resetAtSecondary: secondary?.resetsAt ?? null,
    additionalQuotas: [],
    routingPolicy: source.routingPolicy,
    limitWarmupEnabled: source.limitWarmupEnabled,
    limitWarmup: source.limitWarmup,
    source,
  };
}

export const ACCOUNT_CATEGORIES = ["all", "codex", "claude", "api"] as const;
export type AccountCategory = typeof ACCOUNT_CATEGORIES[number];

export function sourceAccountCategory(source: ModelSource): Exclude<AccountCategory, "all"> {
  if (source.provider === "openai") return "codex";
  return source.provider === "claude" || source.provider === "anthropic" ? "claude" : "api";
}

export function sourceSelectionId(sourceId: string) {
  return `source:${sourceId}`;
}

export function sourceStatus(source: ModelSource, now = Date.now()) {
  if (!source.isEnabled) return "paused";
  if (source.entitlementStatus === "reauth_required") return "reauth_required";
  if (source.entitlementStatus === "blocked") return "deactivated";
  if (source.quotaWindows?.some((window) => !sourceWindowIsStale(window, now) && window.usedRatio !== null && window.usedRatio >= 1)) return "quota_exceeded";
  if (source.cooldownUntil && Date.parse(source.cooldownUntil) > now) return "rate_limited";
  if (source.quotaError || source.healthStatus === "error") return "error";
  if (source.quotaWindows?.some((window) => sourceWindowIsStale(window, now))) return "stale";
  if (source.authType === "claude_oauth") {
    if (source.entitlementStatus !== "verified" || !source.quotaWindows?.length) return "unknown";
    if (source.quotaWindows.some((window) => window.usedRatio === null)) return "unknown";
  }
  return source.healthStatus === "healthy" || source.healthStatus === "active" ? "active" : "unknown";
}
