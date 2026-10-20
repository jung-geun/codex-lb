import { useLayoutEffect, useRef, useState } from "react";
import { Database, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";

import { AlertMessage } from "@/components/alert-message";
import { Button } from "@/components/ui/button";
import { hasPermission, useAuthStore, usePermission } from "@/features/auth/hooks/use-auth";
import { ConnectionGuides } from "@/features/model-sources/components/connection-guides";
import { ModelSourceCreateDialog } from "@/features/model-sources/components/model-source-create-dialog";
import { ModelSourceDetail } from "@/features/model-sources/components/model-source-detail";
import { useModelSources } from "@/features/model-sources/hooks/use-model-sources";
import type { ModelSourceCreateRequest } from "@/features/model-sources/schemas";
import { useDialogState } from "@/hooks/use-dialog-state";
import { getErrorMessageOrNull } from "@/utils/errors";

export type ModelSourcesSettingsProps = {
  disabled?: boolean;
};

export function ModelSourcesSettings(props: ModelSourcesSettingsProps) {
  const canWriteAccounts = usePermission("accounts:write");
  const canWrite = useAuthStore((state) => state.canWrite);
  const writable = !props.disabled && canWriteAccounts && canWrite;
  return <ModelSourcesSettingsSession key={String(writable)} writable={writable} />;
}

function ModelSourcesSettingsSession({ writable }: { writable: boolean }) {
  const { t } = useTranslation();
  const active = useRef(true);
  const [createdOAuthSourceId, setCreatedOAuthSourceId] = useState<string | null>(null);
  const { modelSourcesQuery, createMutation } = useModelSources();
  const createDialog = useDialogState();
  const sources = modelSourcesQuery.data?.sources ?? [];
  const busy = modelSourcesQuery.isFetching || createMutation.isPending;
  const error = getErrorMessageOrNull(modelSourcesQuery.error) || getErrorMessageOrNull(createMutation.error);

  useLayoutEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);

  const canManage = () => {
    const auth = useAuthStore.getState();
    return active.current && writable && auth.canWrite && hasPermission(auth.permissions, "accounts:write");
  };
  const createSource = async (payload: ModelSourceCreateRequest) => {
    try {
      if (!canManage() || busy) {
        throw new Error(t("modelSources.subscription.operationFailed"));
      }
      const source = await createMutation.mutateAsync(payload);
      if (source.authType === "claude_oauth" && canManage()) {
        setCreatedOAuthSourceId(source.id);
      }
    } finally {
      delete payload.apiKey;
      createMutation.reset();
    }
  };

  return (
    <section className="space-y-4 rounded-xl border bg-card p-5">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10">
            <Database className="h-4 w-4 text-primary" aria-hidden="true" />
          </div>
          <div>
            <h3 className="text-sm font-semibold">{t("modelSources.title")}</h3>
            <p className="text-xs text-muted-foreground">{t("modelSources.description")}</p>
          </div>
        </div>
        {writable ? (
          <Button
            type="button"
            size="sm"
            className="h-8 gap-1.5 text-xs"
            disabled={busy}
            onClick={() => {
              if (!canManage() || busy) return;
              createDialog.show();
            }}
          >
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            {t("modelSources.actions.addSource")}
          </Button>
        ) : null}
      </div>

      {error ? <AlertMessage variant="error">{error}</AlertMessage> : null}
      {modelSourcesQuery.isLoading ? <p role="status" className="text-sm text-muted-foreground">{t("modelSources.subscription.loadingSources")}</p> : null}

      <div className="space-y-2">
        {sources.length > 0 ? (
          sources.map((source) => (
            <ModelSourceDetail
              key={source.id}
              source={source}
              readOnly={!writable}
              connectOnMount={!createDialog.open && source.id === createdOAuthSourceId}
            />
          ))
        ) : (
          <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
            {t("modelSources.empty")}
          </div>
        )}
      </div>

      <ConnectionGuides className="border-t pt-4" />

      {createDialog.open && writable ? (
        <ModelSourceCreateDialog
          open
          busy={createMutation.isPending}
          onOpenChange={(open) => {
            if (!open) createDialog.hide();
          }}
          onSubmit={createSource}
        />
      ) : null}
    </section>
  );
}
