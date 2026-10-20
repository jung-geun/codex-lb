import type { AccountSummary } from "@/features/dashboard/schemas";

export function accountSubscriptionCredits(account: Pick<AccountSummary,
  "windowMinutesMonthly" | "windowMinutesPrimary" | "windowMinutesSecondary" |
  "remainingCreditsMonthly" | "remainingCreditsPrimary" | "remainingCreditsSecondary"
>): number | null {
  const monthlyOnly =
    account.windowMinutesMonthly != null &&
    account.windowMinutesPrimary == null &&
    account.windowMinutesSecondary == null;
  const weeklyOnly = account.windowMinutesPrimary == null && account.windowMinutesSecondary != null;

  const value = monthlyOnly
    ? account.remainingCreditsMonthly
    : weeklyOnly
      ? account.remainingCreditsSecondary
      : (account.remainingCreditsSecondary ?? account.remainingCreditsPrimary);

  return value ?? null;
}

export function formatCreditValue(value: number | null | undefined): string {
  return value == null ? "-" : value.toFixed(2);
}

export function formatPurchasedCredits(account: Pick<AccountSummary, "creditsUnlimited" | "creditsBalance">, unlimitedLabel: string): string {
  if (account.creditsUnlimited) {
    return unlimitedLabel;
  }
  return formatCreditValue(account.creditsBalance);
}
