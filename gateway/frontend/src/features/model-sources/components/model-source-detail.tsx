import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import { useTranslation } from "react-i18next";

import { AlertMessage } from "@/components/alert-message";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { hasPermission, useAuthStore, usePermission } from "@/features/auth/hooks/use-auth";
import { refreshSourceQuota, warmupSource } from "@/features/model-sources/api";
import { ModelSourceConnectDialog } from "@/features/model-sources/components/model-source-connect-dialog";
import { ModelSourceEditDialog } from "@/features/model-sources/components/model-source-edit-dialog";
import { usePrivacyStore } from "@/hooks/use-privacy";
import { useModelSources, useModelSourceApiUsage, useModelSourceQuotaHistory, useModelSourceUsage } from "@/features/model-sources/hooks/use-model-sources";
import type { ModelSource, ModelSourceUpdateRequest } from "@/features/model-sources/schemas";
import { AccountUsagePanel } from "@/features/accounts/components/account-usage-panel";
import { AccountActions } from "@/features/accounts/components/account-actions";
import { AccountTokenInfo } from "@/features/accounts/components/account-token-info";
import { sourceToAccountView } from "@/features/accounts/source-selection";
import { formatSlug } from "@/utils/formatters";
import { sourceWindowLabel } from "@/features/model-sources/monitoring";
import { useQuotaEstimates } from "@/features/quota-estimates/use-quota-estimates";
import { withQuotaEstimates } from "@/features/quota-estimates/view";

export type ModelSourceDetailProps = {
  source: ModelSource;
  readOnly?: boolean;
  /** Continue an OAuth source creation through the same guarded connection dialog. */
  connectOnMount?: boolean;
};

type SourceDialog = { sourceId: string; kind: "edit" | "delete" | "connect" };

/** Shared monitoring and management panel. readOnly can restrict, never grant, write access. */
export function ModelSourceDetail(props: ModelSourceDetailProps) {
  const canWriteAccounts = usePermission("accounts:write");
  const canWrite = useAuthStore((state) => state.canWrite);
  const writable = !props.readOnly && canWriteAccounts && canWrite;
  return <ModelSourceDetailSession key={`${props.source.id}:${writable}`} {...props} writable={writable} />;
}

function ModelSourceDetailSession({
  source, connectOnMount = false, writable,
}: ModelSourceDetailProps & { writable: boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const blurred = usePrivacyStore((state) => state.blurred);
  const active = useRef(true);
  const [dialog, setDialog] = useState<SourceDialog | null>(null);
  const connectedOnMount = useRef<string | null>(null);
  const { modelSourcesQuery, updateMutation, deleteMutation } = useModelSources();
  const [usageDays, setUsageDays] = useState<1 | 7 | 30>(1);
  const meteredApi = source.billingMode === "api_metered";
  const usageQuery = useModelSourceUsage(source.id, meteredApi ? usageDays : 1);
  const apiUsageQuery = useModelSourceApiUsage(meteredApi ? source.id : null);
  const historyQuery = useModelSourceQuotaHistory(source.authType === "claude_oauth" ? source.id : null);
  const estimatesQuery = useQuotaEstimates();
  const account = withQuotaEstimates(sourceToAccountView(source), estimatesQuery.data, estimatesQuery.state);
  const quotaMutation = useMutation({
    mutationFn: refreshSourceQuota,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["model-sources", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["model-sources", "quota-history", source.id] });
      void queryClient.invalidateQueries({ queryKey: ["model-sources", "api-usage", source.id] });
      void queryClient.invalidateQueries({ queryKey: ["models"] });
      void queryClient.invalidateQueries({ queryKey: ["quota-estimates"] });
      void queryClient.invalidateQueries({ queryKey: ["api-keys", "list"] });
    },
  });
  const warmupMutation = useMutation({
    mutationFn: warmupSource,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["model-sources"] });
      void queryClient.invalidateQueries({ queryKey: ["quota-estimates"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
  const busy = modelSourcesQuery.isFetching || Boolean(modelSourcesQuery.error) || updateMutation.isPending || deleteMutation.isPending || quotaMutation.isPending || warmupMutation.isPending;
  const error = modelSourcesQuery.error || updateMutation.error || deleteMutation.error || quotaMutation.error || warmupMutation.error;
  const activeDialog = writable && dialog?.sourceId === source.id ? dialog.kind : null;

  useLayoutEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);

  useEffect(() => {
    if (!connectOnMount || !writable || source.authType !== "claude_oauth" || connectedOnMount.current === source.id) return;
    const frame = requestAnimationFrame(() => {
      const auth = useAuthStore.getState();
      if (!active.current || !writable || !auth.canWrite || !hasPermission(auth.permissions, "accounts:write")) return;
      connectedOnMount.current = source.id;
      setDialog({ sourceId: source.id, kind: "connect" });
    });
    return () => cancelAnimationFrame(frame);
  }, [connectOnMount, writable, source.authType, source.id]);

  // Recheck the current session as well as the render-time guard for retained callbacks.
  const canManage = () => {
    const auth = useAuthStore.getState();
    const inventory = queryClient.getQueryState(["model-sources", "list"]);
    return active.current && writable && auth.canWrite && hasPermission(auth.permissions, "accounts:write") && inventory?.fetchStatus !== "fetching" && inventory?.status !== "error";
  };
  const openDialog = (kind: SourceDialog["kind"]) => {
    if (!canManage() || busy) return;
    setDialog({ sourceId: source.id, kind });
  };
  const updateSource = async (sourceId: string, payload: ModelSourceUpdateRequest) => {
    try {
      if (!canManage() || busy || sourceId !== source.id) {
        throw new Error(t("modelSources.subscription.operationFailed"));
      }
      await updateMutation.mutateAsync({ sourceId, payload });
    } finally {
      delete payload.apiKey;
      updateMutation.reset();
    }
  };
  const pricedModel = source.models.find((model) => model.inputPer1M !== null || model.outputPer1M !== null);
  const protocols = [
    source.supportsMessages ? "modelSources.subscription.messages" : null,
    source.supportsCountTokens ? "modelSources.subscription.countTokens" : null,
    source.supportsChatCompletions ? "modelSources.capabilities.chatCompletions" : null,
    source.supportsResponses ? "modelSources.capabilities.responses" : null,
    source.supportsAudioTranscriptions ? "modelSources.capabilities.audioTranscriptions" : null,
    source.supportsEmbeddings ? "modelSources.capabilities.embeddings" : null,
  ].filter((key): key is string => key !== null);
  const subscription = source.billingMode === "subscription" || source.billingMode === "coding_plan";
  const catalogApi = meteredApi && (source.provider === "openai" || source.provider === "anthropic");
  const title = source.accountLabel?.trim() || source.name;

  return (
    <section className="animate-fade-in-up min-w-0 space-y-4 rounded-xl border bg-card p-4 sm:p-5">
      <div>
        <div className="flex min-w-0 items-center gap-1.5">
          <h2 className={`min-w-0 break-all text-base font-semibold${blurred ? " privacy-blur" : ""}`}>{title}</h2>
          {writable ? <Button type="button" size="icon-xs" variant="ghost" disabled={busy} onClick={() => openDialog("edit")}>
            <Pencil className="size-3.5" aria-hidden="true" />
            <span className="sr-only">{t("modelSources.actions.editAria", { name: source.name })}</span>
          </Button> : null}
        </div>
        {title !== source.name ? <p className={`mt-0.5 text-xs text-muted-foreground${blurred ? " privacy-blur" : ""}`}>{source.name}</p> : null}
        <p className={`mt-0.5 break-words text-xs text-muted-foreground${blurred ? " privacy-blur" : ""}`}>
          {source.accountUuid ?? t(`modelSources.subscription.providers.${source.provider ?? "custom"}`)} | {formatSlug(account.planType)}
        </p>
      </div>
      {meteredApi ? <div className="space-y-1 text-xs text-muted-foreground"><p>{t(source.apiFallbackOnly ? "modelSources.fallback.enabled" : "modelSources.fallback.disabled")}</p><p>{t("modelSources.fallback.description")}</p></div> : null}

      {error ? <AlertMessage variant="error">{t("sourceMonitoring.operationFailed")}</AlertMessage> : null}
      <AccountUsagePanel
        account={account}
        sourceUsage={usageQuery.data}
        sourceUsageLoading={usageQuery.isPending}
        sourceUsageError={usageQuery.isError}
        sourceUsageDays={meteredApi ? usageDays : undefined}
        onSourceUsageDaysChange={meteredApi ? setUsageDays : undefined}
        apiUsage={apiUsageQuery.data}
        apiUsageLoading={apiUsageQuery.isPending}
        apiUsageError={apiUsageQuery.isError}
        sourceHistory={historyQuery.data}
        sourceHistoryLoading={source.authType === "claude_oauth" && historyQuery.isPending}
        sourceHistoryError={historyQuery.isError}
      />
      <AccountTokenInfo account={account} />

      <details className="space-y-3 rounded-lg border bg-muted/30 p-4">
        <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wider text-muted-foreground">{t("sourceMonitoring.connectionDetails")}</summary>
        <p className="break-all text-xs text-muted-foreground">{source.baseUrl}</p>
        <div className="flex flex-wrap gap-1.5">{protocols.map((key) => <Badge key={key} variant="secondary">{t(key)}</Badge>)}</div>
        <dl className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
          <div><dt>{t("sourceMonitoring.accountId")}</dt><dd className={`break-all${blurred ? " privacy-blur" : ""}`}>{source.accountUuid ?? t("sourceMonitoring.unknown")}</dd></div>
          <div><dt>{t("sourceMonitoring.organizationId")}</dt><dd className={`break-all${blurred ? " privacy-blur" : ""}`}>{source.organizationUuid ?? t("sourceMonitoring.unknown")}</dd></div>
          <div><dt>{t("sourceMonitoring.connectionHealth")}</dt><dd>{t(`modelSources.detail.health.${source.healthStatus}`, { defaultValue: source.healthStatus })}</dd></div>
          <div><dt>{t("sourceMonitoring.authType")}</dt><dd>{t(`modelSources.subscription.auth.${source.authType ?? "none"}`)}</dd></div>
          <div><dt>{t("sourceMonitoring.extraUsage")}</dt><dd>{t(source.extraUsageEnabled === true ? "sourceMonitoring.extraUsageEnabled" : source.extraUsageEnabled === false ? "sourceMonitoring.extraUsageDisabled" : "sourceMonitoring.unknown")}</dd></div>
        </dl>
        {catalogApi ? <p className="text-xs text-muted-foreground">{t(source.healthStatus === "healthy" && source.quotaCheckedAt && !source.quotaError ? "modelSources.detail.apiCatalogVerified" : "modelSources.detail.apiCatalogUnknown")}</p> : !meteredApi ? <p className="text-xs text-muted-foreground">{t(`modelSources.subscription.entitlement.${source.entitlementStatus ?? "unverified"}`)}</p> : null}
        {source.entitlementDetail ? <p className="text-xs text-muted-foreground">{t("sourceMonitoring.entitlementNotice")}</p> : null}
        <p className={`text-xs text-muted-foreground${blurred ? " privacy-blur" : ""}`}>{t("modelSources.subscription.routing", { priority: source.priority ?? 0, group: source.quotaGroup ?? t("modelSources.subscription.noGroup") })}</p>
        {source.entitlementVerifiedAt ? <p className="text-xs text-muted-foreground">{t("modelSources.subscription.verifiedAt", { date: new Date(source.entitlementVerifiedAt).toLocaleString() })}</p> : null}
        {source.extraUsageEnabled === true ? <p className="text-xs text-destructive">{t("modelSources.subscription.extraUsageWarning")}</p> : null}
        <p className="text-xs text-muted-foreground">{t(source.isEnabled ? "common.states.enabled" : "common.states.disabled")}</p>
        {source.cooldownUntil ? <p className="text-xs text-muted-foreground">{t("modelSources.subscription.cooldown", { date: new Date(source.cooldownUntil).toLocaleString() })}</p> : null}
        {source.provider === "zai" ? <p className="text-xs text-muted-foreground">{t("modelSources.subscription.keyTypeObservation", { declared: t(`modelSources.subscription.keyTypes.${source.keyType ?? "unknown"}`), detected: t(`modelSources.subscription.keyTypes.${source.detectedKeyType ?? "unknown"}`) })}</p> : null}
        {source.quotaWindows?.map((window) => <div key={JSON.stringify([window.windowKey, window.model])} className="space-y-1 text-xs text-muted-foreground">
          <p className="font-medium">{sourceWindowLabel(window, t)}</p>
          {window.usedValue !== null || window.limitValue !== null ? <p>{t("modelSources.detail.quotaValues", { used: window.usedValue ?? t("sourceMonitoring.unknown"), limit: window.limitValue ?? t("sourceMonitoring.unknown") })}</p> : null}
          <p>{t("modelSources.subscription.observedAt", { date: new Date(window.observedAt).toLocaleString() })}</p>
        </div>)}
        <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">{t("modelSources.detail.models")}</h4>
        <div className="flex flex-wrap gap-1.5">
          {source.models.map((model) => (
            <Badge
              key={model.id}
              variant={model.isEnabled ? "outline" : "secondary"}
              className="max-w-full break-all whitespace-normal"
              title={`${t("modelSources.fields.contextWindow")}: ${model.contextWindow ?? t("modelSources.subscription.usageUnknown")} · ${t("modelSources.fields.maxOutputTokens")}: ${model.maxOutputTokens ?? t("modelSources.subscription.usageUnknown")}`}
            >
              {model.model} · {t(model.isEnabled ? "common.states.enabled" : "common.states.disabled")}
              {subscription ? ` · ${t(model.subscriptionRestricted ? "modelSources.subscription.entitlement.blocked" : model.subscriptionEligible ? "modelSources.subscription.entitlement.verified" : "modelSources.subscription.entitlement.unverified")}` : ""}
            </Badge>
          ))}
        </div>
        {source.models.length === 0 ? <p className="text-xs text-muted-foreground">{t("modelSources.detail.noModels")}</p> : null}
        {pricedModel ? (
          <Badge variant="secondary" className="max-w-full whitespace-normal">
            {t(meteredApi ? "modelSources.detail.apiPrices" : "modelSources.subscription.apiEquivalent", {
              price: t("modelSources.detail.tokenPrice", {
                input: pricedModel.inputPer1M ?? t("modelSources.subscription.usageUnknown"),
                output: pricedModel.outputPer1M ?? t("modelSources.subscription.usageUnknown"),
              }),
            })}
          </Badge>
        ) : null}
      </details>

      {writable ? (
        <AccountActions
          account={account}
          busy={busy}
          onPause={() => {
            if (canManage() && !busy) updateMutation.mutate({ sourceId: source.id, payload: { isEnabled: false } });
          }}
          onResume={() => {
            if (canManage() && !busy) updateMutation.mutate({ sourceId: source.id, payload: { isEnabled: true } });
          }}
          onDelete={() => openDialog("delete")}
          onRoutingPolicyChange={(_, routingPolicy) => {
            if (canManage() && !busy) updateMutation.mutate({ sourceId: source.id, payload: { routingPolicy } });
          }}
          onLimitWarmupChange={(_, enabled) => {
            if (canManage() && !busy) updateMutation.mutate({ sourceId: source.id, payload: { limitWarmupEnabled: enabled } });
          }}
          onRefresh={source.provider && source.provider !== "custom" ? () => {
            if (canManage() && !busy) quotaMutation.mutate(source.id);
          } : undefined}
          onWarmupNow={source.warmupSupported ? () => {
            if (canManage() && !busy) warmupMutation.mutate(source.id);
          } : undefined}
          onConnect={source.authType === "claude_oauth" ? () => openDialog("connect") : undefined}
        />
      ) : null}

      {activeDialog === "edit" ? (
        <ModelSourceEditDialog open busy={busy} source={source} onOpenChange={(open) => { if (!open) setDialog(null); }} onSubmit={updateSource} />
      ) : null}
      {activeDialog === "connect" ? (
        <ModelSourceConnectDialog key={source.id} source={source} canManage={canManage} onClose={() => setDialog(null)} />
      ) : null}
      {activeDialog === "delete" ? (
        <ConfirmDialog
          open
          title={t("modelSources.deleteDialog.title")}
          description={t("modelSources.deleteDialog.description")}
          confirmLabel={t("common.actions.delete")}
          confirmDisabled={busy}
          keepOpenOnConfirm
          onOpenChange={(open) => { if (!open) setDialog(null); }}
          onConfirm={() => {
            if (!canManage() || busy) return;
            deleteMutation.mutate(source.id, { onSuccess: () => setDialog(null) });
          }}
        />
      ) : null}
    </section>
  );
}
