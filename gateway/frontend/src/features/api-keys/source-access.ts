import type { BillingPolicy, SourceModelAccess } from "@/features/api-keys/schemas";

export type SourceAccessPolicy = {
  assignedSourceIds: string[];
  sourceAssignmentScopeEnabled: boolean;
  allowedModels: string[] | null;
  enforcedModel: string | null;
  billingPolicy: BillingPolicy;
};

export function effectiveSourceAvailability(
  entry: SourceModelAccess,
  policy: SourceAccessPolicy,
): SourceModelAccess["availability"] {
  if (
    (policy.sourceAssignmentScopeEnabled && !policy.assignedSourceIds.includes(entry.sourceId)) ||
    (policy.allowedModels && policy.allowedModels.length > 0 && !policy.allowedModels.includes(entry.model)) ||
    (policy.enforcedModel && policy.enforcedModel !== entry.model) ||
    (policy.billingPolicy === "subscription_only" && !["subscription", "coding_plan"].includes(entry.billingMode))
  ) {
    return "restricted";
  }
  return entry.availability;
}
