import { useTranslation } from "react-i18next";

import { useSourceAccess } from "@/features/api-keys/hooks/use-source-access";
import { effectiveSourceAvailability, type SourceAccessPolicy } from "@/features/api-keys/source-access";

export function SourceAccessPanel({ policy, keyId }: { policy: SourceAccessPolicy; keyId?: string }) {
  const { t } = useTranslation();
  const query = useSourceAccess(keyId);
  if (query.permitted === false) return null;
  return (
    <section className="space-y-2 rounded-md border p-3 text-xs" aria-label={t("apiKeyMonitoring.accessTitle")}>
      <h3 className="font-semibold">{t("apiKeyMonitoring.accessTitle")}</h3>
      <p className="text-muted-foreground">{t("apiKeyMonitoring.intersection")}</p>
      {query.isPending ? <p>{t("apiKeys.modelSelect.loading")}</p> : null}
      {query.isError ? <p role="status">{t("apiKeyMonitoring.inventoryUnavailable")}</p> : null}
      {query.data?.models.length === 0 ? <p>{t("apiKeyMonitoring.noSources")}</p> : null}
      {query.data?.models.map((entry) => {
        const availability = effectiveSourceAvailability(entry, policy);
        return (
          <div key={`${entry.sourceId}:${entry.model}`} className="space-y-1 border-t pt-2">
            <p><span className="font-medium">{entry.sourceName}</span> · {entry.provider} · {entry.billingMode}</p>
            <p className="break-all font-mono">{entry.model}</p>
            <p>{t(`apiKeyMonitoring.availability.${availability}`)}</p>
            {entry.quotaWindows.filter((window) => window.model === null || window.model === entry.model).map((window) => (
              <p key={`${window.windowKey}:${window.model}`} className="text-muted-foreground">
                {window.windowKey}: {window.usedRatio === null ? t("apiKeyMonitoring.quotaUnknown") : t("apiKeyMonitoring.quotaUsed", { percent: Math.round(window.usedRatio * 100) })}
                {window.resetsAt ? ` · ${t("apiKeyMonitoring.resetsAt", { time: new Date(window.resetsAt).toLocaleString() })}` : ""}
                {` · ${t("apiKeyMonitoring.observedAt", { time: new Date(window.observedAt).toLocaleString() })}`}
              </p>
            ))}
          </div>
        );
      })}
      <p className="text-muted-foreground">{t("apiKeyMonitoring.noFallback")}</p>
    </section>
  );
}
