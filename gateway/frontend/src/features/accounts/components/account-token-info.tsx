import { useTranslation } from "react-i18next";

import type { AccountView } from "@/features/accounts/source-selection";
import { useCurrentTime } from "@/hooks/use-current-time";
import {
  formatAccessTokenLabel,
  formatIdTokenLabel,
  formatRefreshTokenLabel,
} from "@/utils/formatters";

export type AccountTokenInfoProps = {
  account: AccountView;
};

export function AccountTokenInfo({ account }: AccountTokenInfoProps) {
  const { t } = useTranslation();
  const now = useCurrentTime();
  const source = account.source;
  const presenceLabel = (present: boolean | undefined) => t(present === undefined ? "sourceMonitoring.unknown" : present ? "sourceMonitoring.stored" : "sourceMonitoring.notStored");
  const access = !source ? formatAccessTokenLabel(account.auth)
    : source.authType !== "api_key" && source.hasAccessToken === true && source.credentialExpiresAt
      ? formatAccessTokenLabel({ access: { expiresAt: source.credentialExpiresAt } })
      : `${presenceLabel(source.authType === "api_key" ? undefined : source.hasAccessToken)}${source.credentialExpiresAt ? ` · ${t(Date.parse(source.credentialExpiresAt) <= now ? "sourceMonitoring.expiredAt" : "sourceMonitoring.expiresAt", { date: new Date(source.credentialExpiresAt).toLocaleString() })}` : ` · ${t("sourceMonitoring.expiryUnknown")}`}`;
  return (
    <div className="space-y-3 rounded-lg border bg-muted/30 p-4">
      <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">{t("accounts.tokenInfo.title")}</h3>
      <dl className="space-y-2 text-xs">
        <div className="flex min-w-0 items-center justify-between gap-2">
          <dt className="text-muted-foreground">{t(source?.authType === "api_key" ? "sourceMonitoring.apiKey" : "accounts.tokenInfo.access")}</dt>
          <dd className="min-w-0 break-words text-right font-medium">{access}</dd>
        </div>
        <div className="flex min-w-0 items-center justify-between gap-2">
          <dt className="text-muted-foreground">{t("accounts.tokenInfo.refresh")}</dt>
          <dd className="min-w-0 break-words text-right font-medium">{source ? presenceLabel(source.hasRefreshToken) : formatRefreshTokenLabel(account.auth)}</dd>
        </div>
        <div className="flex min-w-0 items-center justify-between gap-2">
          <dt className="text-muted-foreground">{t(source ? "sourceMonitoring.connectionHealth" : "accounts.tokenInfo.idToken")}</dt>
          <dd className="min-w-0 break-words text-right font-medium">{source ? t(`modelSources.detail.health.${source.healthStatus}`, { defaultValue: source.healthStatus }) : formatIdTokenLabel(account.auth)}</dd>
        </div>
      </dl>
    </div>
  );
}
