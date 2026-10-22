import { isClaudeExtraUsageAllowed } from "@/lib/providers/claudeExtraUsage";
import { isCodexPaidCreditsEnabled } from "@/lib/providers/codexPaidCredits";

/** Cached plan-only cutoffs defer explicit billing opt-ins to mandatory live native preflight. */
export function defersQuotaCutoff(
  provider: string | null | undefined,
  providerSpecificData: unknown,
  requestedModel?: string | null
): boolean {
  return (
    isClaudeExtraUsageAllowed(provider, providerSpecificData) ||
    isCodexPaidCreditsEnabled(provider, providerSpecificData, requestedModel)
  );
}

