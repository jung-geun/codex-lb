import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { AlertMessage } from "@/components/alert-message";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { cancelSourceOAuth, completeSourceOAuth, getSourceOAuthStatus, importSourceCredentials, startSourceOAuth } from "@/features/model-sources/api";
import type { ModelSource, SourceOAuthFlow } from "@/features/model-sources/schemas";
import { hasPermission, useAuthStore } from "@/features/auth/hooks/use-auth";
import { getErrorMessageOrNull } from "@/utils/errors";

export function ModelSourceConnectDialog({ source, onClose, canManage }: { source: ModelSource; onClose: () => void; canManage?: () => boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [flow, setFlow] = useState<SourceOAuthFlow | null>(null);
  const [status, setStatus] = useState("pending");
  const [code, setCode] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ModelSource | null>(null);
  const flowRef = useRef<SourceOAuthFlow | null>(null);
  const active = useRef(true);
  const writable = () => {
    const auth = useAuthStore.getState();
    return auth.canWrite && hasPermission(auth.permissions, "accounts:write") && (canManage?.() ?? true);
  };

  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      if (flowRef.current) void cancelSourceOAuth(source.id, flowRef.current.flowId).catch(() => undefined);
    };
  }, [source.id]);

  useEffect(() => {
    if (!flow) return;
    const controller = new AbortController();
    let timer: number | undefined;
    const poll = async () => {
      try {
        const next = await getSourceOAuthStatus(source.id, flow.flowId, controller.signal);
        if (controller.signal.aborted) return;
        setStatus(next.status === "pending" && Date.parse(next.expiresAt) <= Date.now() ? "expired" : next.status);
        setError(next.error ?? null);
        if (next.status === "completed") {
          setCode("");
          void queryClient.invalidateQueries({ queryKey: ["model-sources", "list"] });
          void queryClient.resetQueries({ queryKey: ["model-sources", "quota-history", source.id] });
          void queryClient.resetQueries({ queryKey: ["quota-estimates"] });
          void queryClient.invalidateQueries({ queryKey: ["models"] });
          void queryClient.invalidateQueries({ queryKey: ["api-keys", "list"] });
        }
        if ((next.status === "pending" || next.status === "exchanging") && Date.parse(next.expiresAt) > Date.now()) {
          timer = window.setTimeout(() => void poll(), 3000);
        }
      } catch (cause) {
        if (!controller.signal.aborted) setError(getErrorMessageOrNull(cause) ?? t("modelSources.subscription.operationFailed"));
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [flow, source.id, queryClient, t]);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["model-sources", "list"] });
    void queryClient.resetQueries({ queryKey: ["model-sources", "quota-history", source.id] });
    void queryClient.resetQueries({ queryKey: ["quota-estimates"] });
    void queryClient.invalidateQueries({ queryKey: ["models"] });
    void queryClient.invalidateQueries({ queryKey: ["api-keys", "list"] });
  };

  const run = async (operation: () => Promise<void>) => {
    if (!active.current || !writable()) return;
    setBusy(true);
    setError(null);
    try { await operation(); }
    catch (cause) {
      if (active.current) setError(getErrorMessageOrNull(cause) ?? t("modelSources.subscription.operationFailed"));
    } finally { if (active.current) setBusy(false); }
  };

  const accept = (updated: ModelSource) => {
    flowRef.current = null;
    if (active.current) { setFlow(null); setResult(updated); }
    invalidate();
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("modelSources.subscription.connectTitle", { name: source.name })}</DialogTitle>
          <DialogDescription>{t("modelSources.subscription.oauthDescription")}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4" aria-busy={busy}>
          {error ? <AlertMessage variant="error">{error}</AlertMessage> : null}
          {result ? <p role="status" className="text-sm">{t("modelSources.subscription.savedStatus", { status: t(`modelSources.subscription.entitlement.${result.entitlementStatus ?? "unverified"}`) })}</p> : null}
          {!flow ? (
            <Button disabled={busy} onClick={() => void run(async () => {
              const started = await startSourceOAuth(source.id);
              if (!active.current || !writable()) { await cancelSourceOAuth(source.id, started.flowId); return; }
              flowRef.current = started;
              setFlow(started);
              setStatus("pending");
              setResult(null);
            })}>{t("modelSources.subscription.startOAuth")}</Button>
          ) : (
            <div className="space-y-3 rounded-lg border p-3">
              <p role="status" className="text-sm">{t("modelSources.subscription.flowStatus", { status: t(`modelSources.subscription.oauthStatus.${status}`, { defaultValue: status }), expires: new Date(flow.expiresAt).toLocaleString() })}</p>
              <Button asChild variant="outline"><a href={flow.authorizationUrl} target="_blank" rel="noopener noreferrer">{t("modelSources.subscription.openAuthorization")}</a></Button>
              <form className="space-y-3" onSubmit={(event) => {
                event.preventDefault();
                void run(async () => {
                  try { accept(await completeSourceOAuth(source.id, flow.flowId, code.trim())); }
                  finally { if (active.current) setCode(""); }
                });
              }}>
                <label className="block space-y-1 text-sm">
                  <span>{t("modelSources.subscription.authorizationCode")}</span>
                  <Input type="password" autoComplete="off" value={code} onChange={(event) => setCode(event.target.value)} disabled={busy || status !== "pending"} />
                </label>
                <div className="flex flex-wrap gap-2">
                  <Button type="submit" disabled={busy || !code.trim() || status !== "pending"}>{t("modelSources.subscription.completeOAuth")}</Button>
                  <Button type="button" variant="outline" disabled={busy} onClick={() => void run(async () => {
                    await cancelSourceOAuth(source.id, flow.flowId);
                    flowRef.current = null;
                    setFlow(null);
                    setCode("");
                  })}>{t("modelSources.subscription.cancelOAuth")}</Button>
                </div>
              </form>
            </div>
          )}
          <div className="space-y-3 border-t pt-4">
            <h4 className="text-sm font-medium">{t("modelSources.subscription.importTitle")}</h4>
            <p className="text-xs text-muted-foreground">{t("modelSources.subscription.importDescription")}</p>
            <label className="flex items-start gap-2 text-sm">
              <Checkbox checked={confirmed} disabled={busy || !!flow} onCheckedChange={(value) => setConfirmed(value === true)} />
              <span>{t("modelSources.subscription.singleWriter")}</span>
            </label>
            <label className="block space-y-1 text-sm">
              <span>{t("modelSources.subscription.credentialsFile")}</span>
              <Input type="file" accept=".json,application/json" disabled={busy || !confirmed || !!flow} onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = "";
                if (!file) return;
                void run(async () => {
                  let parsed: unknown;
                  try { parsed = JSON.parse(await file.text()); }
                  catch { throw new Error(t("modelSources.subscription.invalidCredentials")); }
                  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(t("modelSources.subscription.invalidCredentials"));
                  if (!active.current || !writable()) return;
                  accept(await importSourceCredentials(source.id, parsed as Record<string, unknown>));
                });
              }} />
            </label>
          </div>
          {busy ? <p role="status" className="text-sm text-muted-foreground">{t("modelSources.subscription.working")}</p> : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
