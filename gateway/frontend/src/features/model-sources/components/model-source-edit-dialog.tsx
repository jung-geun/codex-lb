import { useReducer, useState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { AlertMessage } from "@/components/alert-message";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Form } from "@/components/ui/form";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ModelSourceFormFields } from "@/features/model-sources/components/model-source-form-fields";
import {
  createModelSourceFormSchema,
  draftFromSource,
  mergeReasoningMetadata,
  modelIdsToInput,
  modelSourceDraftReducer,
  type ModelSourceDraft,
  type ModelSourceFormValues,
} from "@/features/model-sources/components/model-source-form";
import type {
  ModelSource,
  ModelSourceModel,
  ModelSourceUpdateRequest,
  ModelSourceModelInput,
} from "@/features/model-sources/schemas";

type ModelDraftChangeFlags = {
  contextWindow: boolean;
  maxOutputTokens: boolean;
  inputPer1M: boolean;
  cachedInputPer1M: boolean;
  outputPer1M: boolean;
  audioPerMinute: boolean;
  supportsStreaming: boolean;
  supportsTools: boolean;
  supportsVision: boolean;
  supportsReasoning: boolean;
};

function parsePositiveInt(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number.parseInt(trimmed, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function parseNonNegativeFloat(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number.parseFloat(trimmed);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function splitModelIds(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((model) => model.trim())
    .filter(Boolean);
}

function modelIdsMatch(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false;
  return left.every((model, index) => model === right[index]);
}

function getModelDraftChangeFlags(
  draft: ModelSourceDraft,
  initialDraft: ModelSourceDraft,
): ModelDraftChangeFlags {
  return {
    contextWindow: draft.contextWindow !== initialDraft.contextWindow,
    maxOutputTokens: draft.maxOutputTokens !== initialDraft.maxOutputTokens,
    inputPer1M: draft.inputPer1M !== initialDraft.inputPer1M,
    cachedInputPer1M: draft.cachedInputPer1M !== initialDraft.cachedInputPer1M,
    outputPer1M: draft.outputPer1M !== initialDraft.outputPer1M,
    audioPerMinute: draft.audioPerMinute !== initialDraft.audioPerMinute,
    supportsStreaming: draft.supportsStreaming !== initialDraft.supportsStreaming,
    supportsTools: draft.supportsTools !== initialDraft.supportsTools,
    supportsVision: draft.supportsVision !== initialDraft.supportsVision,
    supportsReasoning:
      draft.supportsReasoning !== initialDraft.supportsReasoning ||
      JSON.stringify(draft.reasoningEfforts) !== JSON.stringify(initialDraft.reasoningEfforts) ||
      draft.defaultReasoningEffort !== initialDraft.defaultReasoningEffort,
  };
}

function hasAnyModelDraftChange(flags: ModelDraftChangeFlags): boolean {
  return Object.values(flags).some(Boolean);
}

function buildModelInputs(
  modelNames: string[],
  draft: ModelSourceDraft,
  draftChangeFlags: ModelDraftChangeFlags,
  existingModelsByName: Map<string, ModelSourceModel>,
): ModelSourceModelInput[] {
  return modelNames.map((model) => {
    const existingModel = existingModelsByName.get(model);

    return {
      model,
      displayName: existingModel?.displayName ?? model,
      upstreamModel: existingModel?.upstreamModel,
      ...(existingModel?.subscriptionRestricted ? { subscriptionEligible: false } : {}),
      cacheCreationPer1M: existingModel?.cacheCreationPer1M,
      cacheCreation1HPer1M: existingModel?.cacheCreation1HPer1M,
      contextWindow: draftChangeFlags.contextWindow
        ? parsePositiveInt(draft.contextWindow)
        : existingModel?.contextWindow ?? null,
      maxOutputTokens: draftChangeFlags.maxOutputTokens
        ? parsePositiveInt(draft.maxOutputTokens)
        : existingModel?.maxOutputTokens ?? null,
      supportsStreaming: draftChangeFlags.supportsStreaming
        ? draft.supportsStreaming
        : existingModel?.supportsStreaming ?? true,
      supportsTools: draftChangeFlags.supportsTools
        ? draft.supportsTools
        : existingModel?.supportsTools ?? false,
      supportsVision: draftChangeFlags.supportsVision
        ? draft.supportsVision
        : existingModel?.supportsVision ?? false,
      inputPer1M: draftChangeFlags.inputPer1M
        ? parseNonNegativeFloat(draft.inputPer1M)
        : existingModel?.inputPer1M ?? null,
      cachedInputPer1M: draftChangeFlags.cachedInputPer1M
        ? parseNonNegativeFloat(draft.cachedInputPer1M)
        : existingModel?.cachedInputPer1M ?? null,
      outputPer1M: draftChangeFlags.outputPer1M
        ? parseNonNegativeFloat(draft.outputPer1M)
        : existingModel?.outputPer1M ?? null,
      audioPerMinute: draftChangeFlags.audioPerMinute
        ? parseNonNegativeFloat(draft.audioPerMinute)
        : existingModel?.audioPerMinute ?? null,
      rawMetadataJson: draftChangeFlags.supportsReasoning
        ? mergeReasoningMetadata(
            existingModel?.rawMetadataJson,
            draft.supportsReasoning,
            draft.reasoningEfforts,
            draft.defaultReasoningEffort,
          )
        : existingModel?.rawMetadataJson ?? null,
      isEnabled: existingModel?.isEnabled ?? true,
    };
  });
}

export type ModelSourceEditDialogProps = {
  open: boolean;
  busy: boolean;
  source: ModelSource | null;
  onOpenChange: (open: boolean) => void;
  onSubmit: (sourceId: string, payload: ModelSourceUpdateRequest) => Promise<void>;
};

type ModelSourceEditFormProps = {
  source: ModelSource;
  busy: boolean;
  onSubmit: (sourceId: string, payload: ModelSourceUpdateRequest) => Promise<void>;
  onClose: () => void;
};

function ModelSourceEditForm({ source, busy, onSubmit, onClose }: ModelSourceEditFormProps) {
  const { t } = useTranslation();
  const form = useForm<ModelSourceFormValues>({
    resolver: zodResolver(createModelSourceFormSchema(t, source.provider !== undefined && source.provider !== "custom")),
    defaultValues: {
      name: source.name,
      baseUrl: source.baseUrl,
      apiKey: "",
      models: modelIdsToInput(source),
    },
  });
  const [draft, updateDraft] = useReducer(modelSourceDraftReducer, source, draftFromSource);
  const meteredApi = source.billingMode === "api_metered"
    && (source.provider === "openai" || source.provider === "anthropic");
  const [selectedModelId, setSelectedModelId] = useState(source.models[0]?.model ?? "");
  const [modelDrafts, setModelDrafts] = useState<Record<string, ModelSourceDraft>>({});
  const selectModel = (modelId: string) => {
    const model = source.models.find((item) => item.model === modelId);
    if (!model || modelId === selectedModelId) return;
    setModelDrafts((current) => ({ ...current, [selectedModelId]: draft }));
    updateDraft({
      ...(modelDrafts[modelId] ?? draftFromSource(source, model)),
      priority: draft.priority,
      quotaGroup: draft.quotaGroup,
      keyType: draft.keyType,
      apiFallbackOnly: draft.apiFallbackOnly,
    });
    setSelectedModelId(modelId);
  };

  const handleSubmit = async (values: ModelSourceFormValues) => {
    const initialDraft = draftFromSource(source);
    const draftChangeFlags = getModelDraftChangeFlags(draft, initialDraft);
    const sourceModelIds = source.models.map((model) => model.model);
    const modelNames = splitModelIds(values.models);
    const modelIdsChanged = !modelIdsMatch(modelNames, sourceModelIds);

    const payload: ModelSourceUpdateRequest = {
      name: values.name,
      baseUrl: values.baseUrl,
      supportsMessages: draft.supportsMessages,
      supportsCountTokens: draft.supportsCountTokens,
      priority: Number(draft.priority),
      quotaGroup: draft.quotaGroup.trim() || null,
      keyType: draft.keyType,
      apiFallbackOnly: draft.apiFallbackOnly,
      supportsChatCompletions: draft.supportsChatCompletions,
      supportsResponses: draft.supportsResponses,
      supportsAudioTranscriptions: draft.supportsAudioTranscriptions,
      supportsEmbeddings: draft.supportsEmbeddings,
    };

    if (meteredApi) {
      const drafts = { ...modelDrafts, [selectedModelId]: draft };
      const changes = source.models.map((model) => {
        const initial = draftFromSource(source, model);
        const current = drafts[model.model] ?? initial;
        return { model: model.model, draft: current, flags: getModelDraftChangeFlags(current, initial) };
      });
      if (changes.some((item) => hasAnyModelDraftChange(item.flags))) {
        const existingModelsByName = new Map(source.models.map((model) => [model.model, model]));
        payload.models = changes.flatMap((item) =>
          buildModelInputs([item.model], item.draft, item.flags, existingModelsByName),
        );
      }
    } else if (modelIdsChanged || hasAnyModelDraftChange(draftChangeFlags)) {
      const existingModelsByName = new Map(source.models.map((model) => [model.model, model]));
      payload.models = buildModelInputs(
        modelNames,
        draft,
        draftChangeFlags,
        existingModelsByName,
      );
    }

    // The stored key is never returned, so a blank field means "keep it";
    // only a typed value updates the credential.
    const apiKey = values.apiKey.trim();
    if (apiKey) {
      payload.apiKey = apiKey;
    }
    try {
      form.clearErrors("root");
      await onSubmit(source.id, payload);
    } catch {
      form.setError("root", { message: t("modelSources.subscription.operationFailed") });
      return;
    } finally {
      form.setValue("apiKey", "");
    }
    onClose();
  };

  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit(handleSubmit)} aria-busy={busy || form.formState.isSubmitting} className="flex min-h-0 flex-1 flex-col">
        <div
          className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain px-6 pb-4"
          data-testid="model-source-edit-scroll-region"
        >
          {form.formState.errors.root?.message ? <AlertMessage variant="error">{form.formState.errors.root.message}</AlertMessage> : null}
          {meteredApi && source.models.length > 0 ? (
            <div className="space-y-2 rounded-md border p-3">
              <label className="block space-y-1 text-sm font-medium">
                <span>{t("modelSources.fields.catalogModel")}</span>
                <Select value={selectedModelId} onValueChange={selectModel} disabled={busy || form.formState.isSubmitting}>
                  <SelectTrigger className="w-full" aria-label={t("modelSources.fields.catalogModel")}><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {source.models.map((model) => <SelectItem key={model.model} value={model.model}>{model.model}</SelectItem>)}
                  </SelectContent>
                </Select>
              </label>
              <p className="text-xs text-muted-foreground">{t("modelSources.fields.catalogModelDescription")}</p>
            </div>
          ) : null}
          <ModelSourceFormFields
            control={form.control}
            draft={draft}
            updateDraft={updateDraft}
            apiKeyLabel={t("modelSources.fields.upstreamApiKey")}
            hideApiKey={source.authType === "claude_oauth"}
            providerManaged={source.provider !== undefined && source.provider !== "custom"}
            editableModelMetadata={meteredApi && source.models.length > 0}
            showKeyType={source.provider === "zai"}
            showApiFallbackOnly={source.billingMode === "api_metered"}
            catalogModels={source.models}
            apiKeyPlaceholder={t("modelSources.editDialog.keepCurrentKey")}
          />
        </div>
        <DialogFooter className="shrink-0 border-t px-6 py-4">
          {busy || form.formState.isSubmitting ? <p role="status" className="text-xs text-muted-foreground">{t("modelSources.subscription.working")}</p> : null}
          <Button type="submit" disabled={busy || form.formState.isSubmitting}>
	            {t("common.actions.save")}
          </Button>
        </DialogFooter>
      </form>
    </Form>
  );
}

export function ModelSourceEditDialog({
  open,
  busy,
  source,
  onOpenChange,
  onSubmit,
}: ModelSourceEditDialogProps) {
  const { t } = useTranslation();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 overflow-clip p-0 sm:max-w-2xl">
        <DialogHeader className="shrink-0 px-6 pt-6 pr-12 pb-2">
	          <DialogTitle>{t("modelSources.editDialog.title")}</DialogTitle>
	          <DialogDescription>{t("modelSources.editDialog.description")}</DialogDescription>
        </DialogHeader>

        {source ? (
          <ModelSourceEditForm
            key={`${source.id}:${open ? "open" : "closed"}`}
            source={source}
            busy={busy}
            onSubmit={onSubmit}
            onClose={() => onOpenChange(false)}
          />
        ) : (
          <p className="px-6 pb-6 text-sm text-muted-foreground">{t("modelSources.editDialog.selectSource")}</p>
        )}
      </DialogContent>
    </Dialog>
  );
}
