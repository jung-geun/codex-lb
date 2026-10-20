import type { AccountView } from "@/features/accounts/source-selection";
import type { QuotaEstimatesResponse } from "@/features/quota-estimates/schemas";

export function withQuotaEstimates(
  account: AccountView,
  data: QuotaEstimatesResponse | undefined,
  state: AccountView["quotaEstimateState"],
): AccountView {
  const sourceId = account.source?.id;
  const source = sourceId ? data?.sources.find((entry) => entry.sourceId === sourceId) : undefined;
  const native = !sourceId ? data?.accounts.find((entry) => entry.accountId === account.accountId) : undefined;
  return {
    ...account,
    quotaEstimates: source?.windows ?? native?.windows,
    quotaEstimateState: state,
    quotaSharedCount: source?.sharedSourceIds.length ?? native?.sharedAccountIds.length,
  };
}
