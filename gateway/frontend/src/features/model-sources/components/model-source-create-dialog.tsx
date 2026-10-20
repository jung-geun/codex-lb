import { useLayoutEffect, useReducer, useRef, useState } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AlertMessage } from "@/components/alert-message";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { listSourcePresets } from "@/features/model-sources/api";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Form } from "@/components/ui/form";
import { ModelSourceFormFields } from "@/features/model-sources/components/model-source-form-fields";
import {
  initialModelSourceDraft,
  createModelSourceFormSchema,
  modelInputsFromForm,
  modelSourceDraftReducer,
  type ModelSourceFormValues,
} from "@/features/model-sources/components/model-source-form";
import type { ModelSourceCreateRequest, SourcePreset } from "@/features/model-sources/schemas";

const emptyFormValues: ModelSourceFormValues = { name: "", baseUrl: "", apiKey: "", models: "" };

export type ModelSourceCreateDialogProps = {
  open: boolean;
  busy: boolean;
  initialProvider?: NonNullable<ModelSourceCreateRequest["provider"]>;
  onOpenChange: (open: boolean) => void;
  onSubmit: (payload: ModelSourceCreateRequest) => Promise<void>;
};

function presetValues(preset?: SourcePreset): ModelSourceFormValues {
  return preset
    ? { name: preset.name, baseUrl: preset.baseUrl, apiKey: "", models: preset.models.join(", ") }
    : emptyFormValues;
}

function presetDraft(preset?: SourcePreset) {
  return preset ? {
    ...initialModelSourceDraft,
    supportsMessages: preset.supportsMessages,
    supportsCountTokens: preset.supportsCountTokens,
    supportsChatCompletions: preset.supportsChatCompletions,
    supportsResponses: preset.supportsResponses,
    supportsAudioTranscriptions: preset.supportsAudioTranscriptions,
    supportsEmbeddings: preset.supportsEmbeddings,
    supportsStreaming: false,
  } : initialModelSourceDraft;
}

export function ModelSourceCreateDialog(props: ModelSourceCreateDialogProps) {
  const { t } = useTranslation();
  const presets = useQuery({ queryKey: ["model-sources", "presets"], queryFn: listSourcePresets, enabled: props.open });
  if (!props.open) return null;
  const waitingForProvider = props.initialProvider && props.initialProvider !== "custom"
    && !presets.data?.some((preset) => preset.provider === props.initialProvider);
  return (
    <Dialog open onOpenChange={props.onOpenChange}>
      <DialogContent className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 overflow-clip p-0 sm:max-w-2xl">
        <DialogHeader className="shrink-0 px-6 pt-6 pr-12 pb-2">
          <DialogTitle>{t("modelSources.createDialog.title")}</DialogTitle>
          <DialogDescription>{t("modelSources.createDialog.description")}</DialogDescription>
        </DialogHeader>
        {waitingForProvider ? (
          <div className="px-6 pb-6">
            {presets.isLoading
              ? <p role="status">{t("modelSources.subscription.loadingPresets")}</p>
              : <AlertMessage variant="error">{t("modelSources.subscription.presetsUnavailable")}</AlertMessage>}
          </div>
        ) : <ModelSourceCreateForm key={props.initialProvider ?? "custom"} {...props} presets={presets} />}
      </DialogContent>
    </Dialog>
  );
}

function ModelSourceCreateForm({
  busy,
  initialProvider,
  onOpenChange,
  onSubmit,
  presets,
}: ModelSourceCreateDialogProps & { presets: UseQueryResult<SourcePreset[], Error> }) {
  const { t } = useTranslation();
  const [presetId, setPresetId] = useState<string>(initialProvider ?? "custom");
  const initialPreset = presets.data?.find((item) => item.provider === (initialProvider ?? "custom"));
  const session = useRef(0);
  const simple = initialProvider === "openai" || initialProvider === "anthropic" || initialProvider === "claude";
  const requiresApiKey = presetId === "openai" || presetId === "anthropic";
  const formSchema = createModelSourceFormSchema(t, presetId !== "custom");
  const form = useForm<ModelSourceFormValues>({
    resolver: zodResolver(formSchema.extend({
      apiKey: formSchema.shape.apiKey.refine(
        (value) => !requiresApiKey || value.trim().length > 0,
        t("modelSources.validation.apiKeyRequired"),
      ),
    })),
    defaultValues: presetValues(initialPreset),
  });
  const [draft, updateDraft] = useReducer(modelSourceDraftReducer, initialPreset, presetDraft);
  const [error, setError] = useState<string | null>(null);
  const preset = presets.data?.find((item) => item.provider === presetId);
  const presetUnavailable = presetId !== "custom" && !preset;
  const submitting = busy || form.formState.isSubmitting;

  useLayoutEffect(() => {
    session.current += 1;
    return () => {
      session.current += 1;
      form.setValue("apiKey", "");
    };
  }, [form]);

  const selectPreset = (id: string) => {
    setPresetId(id);
    const selected = presets.data?.find((item) => item.provider === id);
    form.reset(presetValues(selected));
    updateDraft(presetDraft(selected));
    setError(null);
  };

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) {
      session.current += 1;
      form.reset(emptyFormValues);
    }
    onOpenChange(nextOpen);
  };

  const handleSubmit = async (values: ModelSourceFormValues) => {
    if (presetUnavailable) {
      setError(t("modelSources.subscription.presetsUnavailable"));
      form.setValue("apiKey", "");
      return;
    }
    const submissionSession = session.current;
    const payload: ModelSourceCreateRequest = {
      provider: preset?.provider ?? "custom",
      authType: preset?.authType ?? (values.apiKey.trim() ? "api_key" : "none"),
      billingMode: preset?.billingMode ?? "unknown",
      apiFallbackOnly: draft.apiFallbackOnly,
      keyType: draft.keyType,
      messagesBaseUrl: preset?.messagesBaseUrl,
      supportsMessages: draft.supportsMessages,
      supportsCountTokens: draft.supportsCountTokens,
      priority: Number(draft.priority),
      quotaGroup: draft.quotaGroup.trim() || null,
      name: values.name,
      baseUrl: values.baseUrl,
      apiKey: values.apiKey.trim() ? values.apiKey.trim() : undefined,
      supportsChatCompletions: draft.supportsChatCompletions,
      supportsResponses: draft.supportsResponses,
      supportsAudioTranscriptions: draft.supportsAudioTranscriptions,
      supportsEmbeddings: draft.supportsEmbeddings,
      models: preset ? [] : modelInputsFromForm(values, draft),
    };
    setError(null);
    try {
      await onSubmit(payload);
      if (session.current === submissionSession) handleOpenChange(false);
    } catch {
      if (session.current === submissionSession) setError(t("modelSources.subscription.operationFailed"));
    } finally {
      payload.apiKey = undefined;
      values.apiKey = "";
      if (session.current === submissionSession) form.setValue("apiKey", "");
    }
  };

  return (
        <Form {...form}>
          <form onSubmit={(event) => { void form.handleSubmit(handleSubmit)(event); }} aria-busy={submitting || (presetUnavailable && presets.isLoading)} className="flex min-h-0 flex-1 flex-col">
            <div
              className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain px-6 pb-4"
              data-testid="model-source-create-scroll-region"
            >
              {presets.isLoading ? <p role="status" className="text-sm text-muted-foreground">{t("modelSources.subscription.loadingPresets")}</p> : null}
              {presets.isError || (presets.isSuccess && presets.data.length === 0) || (presetUnavailable && !presets.isLoading) || error ? <AlertMessage variant="error">{error ?? t("modelSources.subscription.presetsUnavailable")}</AlertMessage> : null}
              {!simple ? (
              <label className="block space-y-1 text-sm font-medium">
                <span>{t("modelSources.subscription.preset")}</span>
                <Select value={presetId} onValueChange={selectPreset} disabled={submitting || presets.isLoading}>
                  <SelectTrigger className="w-full" aria-label={t("modelSources.subscription.preset")}><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="custom">{t("modelSources.subscription.providers.custom")}</SelectItem>
                    {presets.data?.filter((item) => item.provider !== "custom").map((item) => <SelectItem key={item.provider} value={item.provider}>{item.name}</SelectItem>)}
                  </SelectContent>
                </Select>
              </label>
              ) : null}
              {preset ? <p className="text-sm text-muted-foreground">{preset.connectionNote}</p> : null}
              {preset ? <Badge variant="outline">{t(`modelSources.subscription.billing.${preset.billingMode}`)}</Badge> : null}
              {preset ? <p className="text-xs text-muted-foreground">{t("modelSources.subscription.catalogDiscovery")}</p> : null}
              <p className="text-xs text-muted-foreground">{t(requiresApiKey || preset?.billingMode === "api_metered" ? "modelSources.registration.apiKeyNotice" : "modelSources.subscription.unverifiedNotice")}</p>
              <fieldset disabled={submitting || presetUnavailable} className="space-y-4">
              <ModelSourceFormFields
                control={form.control}
                draft={draft}
                updateDraft={updateDraft}
                apiKeyLabel={t("modelSources.fields.upstreamApiKey")}
                hideApiKey={initialProvider === "claude" || preset?.authType === "claude_oauth"}
                providerManaged={!!preset && preset.provider !== "custom"}
                showKeyType={preset?.provider === "zai"}
                showApiFallbackOnly={preset?.billingMode === "api_metered"}
                simple={simple}
              />
              </fieldset>
            </div>
            <DialogFooter className="shrink-0 border-t px-6 py-4">
              {busy || form.formState.isSubmitting ? <p role="status" className="text-xs text-muted-foreground">{t("modelSources.subscription.working")}</p> : null}
              <Button type="submit" disabled={submitting || presetUnavailable}>
	                {t("common.actions.create")}
              </Button>
            </DialogFooter>
          </form>
        </Form>
  );
}
