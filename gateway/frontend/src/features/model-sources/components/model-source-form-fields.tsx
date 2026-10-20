import { useId } from "react";
import type { Control } from "react-hook-form";
import { useTranslation } from "react-i18next";

import { Checkbox } from "@/components/ui/checkbox";
import { FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import type {
  ModelSourceDraft,
  ModelSourceFormValues,
} from "@/features/model-sources/components/model-source-form";
import { parseReasoningEffortsInput } from "@/features/model-sources/components/model-source-form";
import type { ModelSourceModel } from "@/features/model-sources/schemas";
import { SourceKeyTypeSchema } from "@/features/model-sources/schemas";
import { Badge } from "@/components/ui/badge";

type ModelSourceFormFieldsProps = {
  control: Control<ModelSourceFormValues>;
  draft: ModelSourceDraft;
  updateDraft: (patch: Partial<ModelSourceDraft>) => void;
  apiKeyLabel: string;
  apiKeyPlaceholder?: string;
  hideApiKey?: boolean;
  providerManaged?: boolean;
  editableModelMetadata?: boolean;
  showKeyType?: boolean;
  showApiFallbackOnly?: boolean;
  simple?: boolean;
  catalogModels?: ModelSourceModel[];
};

const SOURCE_CAPABILITY_TOGGLES = [
  ["supportsMessages", "modelSources.subscription.messages"] as const,
  ["supportsCountTokens", "modelSources.subscription.countTokens"] as const,
  ["supportsChatCompletions", "modelSources.capabilities.chatCompletions"] as const,
  ["supportsResponses", "modelSources.capabilities.responses"] as const,
  ["supportsAudioTranscriptions", "modelSources.capabilities.audioTranscriptions"] as const,
  ["supportsEmbeddings", "modelSources.capabilities.embeddings"] as const,
];
const SOURCE_CAPABILITY_KEYS: Record<string, true> = Object.fromEntries(
  SOURCE_CAPABILITY_TOGGLES.map(([key]) => [key, true] as const),
);

const CAPABILITY_TOGGLES = [
  ...SOURCE_CAPABILITY_TOGGLES,
  ["supportsStreaming", "modelSources.capabilities.streaming"] as const,
  ["supportsTools", "modelSources.capabilities.tools"] as const,
  ["supportsVision", "modelSources.capabilities.vision"] as const,
  ["supportsReasoning", "modelSources.capabilities.reasoning"] as const,
];

const DEFAULT_REASONING_EFFORTS = ["low", "medium", "high"];

export function ModelSourceFormFields({
  control,
  draft,
  updateDraft,
  apiKeyLabel,
  apiKeyPlaceholder,
  hideApiKey = false,
  providerManaged = false,
  editableModelMetadata = false,
  showKeyType = false,
  showApiFallbackOnly = false,
  simple = false,
  catalogModels = [],
}: ModelSourceFormFieldsProps) {
  const { t } = useTranslation();
  const fallbackDescriptionId = useId();
  const metadataManaged = providerManaged && !editableModelMetadata;
  return (
    <>
      <div className={simple ? "grid gap-3" : "grid gap-3 sm:grid-cols-2"}>
        <FormField
          control={control}
          name="name"
          render={({ field }) => (
            <FormItem>
	              <FormLabel>{t("apiKeys.table.name")}</FormLabel>
              <FormControl>
                <Input {...field} autoComplete="off" />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />

        {!simple ? (
        <FormField
          control={control}
          name="baseUrl"
          render={({ field }) => (
            <FormItem>
	              <FormLabel>{t("modelSources.fields.baseUrl")}</FormLabel>
              <FormControl>
                <Input {...field} disabled={providerManaged} placeholder="https://api.example.com/v1" autoComplete="off" />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        ) : null}
      </div>

      {!simple ? <>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1 text-sm font-medium">
          <span>{t("modelSources.subscription.priority")}</span>
          <Input type="number" step="1" value={draft.priority} onChange={(event) => updateDraft({ priority: event.target.value })} />
        </label>
        <label className="space-y-1 text-sm font-medium">
          <span>{t("modelSources.subscription.quotaGroup")}</span>
          <Input maxLength={128} pattern={"[A-Za-z0-9][A-Za-z0-9:_.\\-]*"} title={t("modelSources.subscription.quotaGroupFormat")} value={draft.quotaGroup} onChange={(event) => updateDraft({ quotaGroup: event.target.value })} autoComplete="off" />
        </label>
      </div>
      <p className="text-xs text-muted-foreground">{t("modelSources.subscription.quotaGroupDescription")}</p>
      </> : null}

      {!simple && showKeyType ? (
        <div className="space-y-2">
          <label className="block space-y-1 text-sm font-medium">
            <span>{t("modelSources.subscription.keyType")}</span>
            <Select value={draft.keyType} onValueChange={(value) => updateDraft({ keyType: SourceKeyTypeSchema.parse(value) })}>
              <SelectTrigger className="w-full" aria-label={t("modelSources.subscription.keyType")}><SelectValue /></SelectTrigger>
              <SelectContent>
                {SourceKeyTypeSchema.options.map((value) => <SelectItem key={value} value={value}>{t(`modelSources.subscription.keyTypes.${value}`)}</SelectItem>)}
              </SelectContent>
            </Select>
          </label>
          <p className="text-xs text-muted-foreground">{t("modelSources.subscription.keyTypeDescription")}</p>
        </div>
      ) : null}

      {!hideApiKey ? (
      <FormField
        control={control}
        name="apiKey"
        render={({ field }) => (
          <FormItem>
            <FormLabel>{apiKeyLabel}</FormLabel>
            <FormControl>
              <Input {...field} type="password" autoComplete="new-password" placeholder={apiKeyPlaceholder} required={simple} />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />
      ) : null}

      {showApiFallbackOnly ? (
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1">
            <p className="text-sm font-medium">{t("modelSources.fallback.label")}</p>
            <p id={fallbackDescriptionId} className="text-xs text-muted-foreground">{t("modelSources.fallback.description")}</p>
          </div>
          <Switch
            aria-label={t("modelSources.fallback.label")}
            aria-describedby={fallbackDescriptionId}
            checked={draft.apiFallbackOnly}
            onCheckedChange={(apiFallbackOnly) => updateDraft({ apiFallbackOnly })}
          />
        </div>
      ) : null}

      {!simple ? <>

      <FormField
        control={control}
        name="models"
        render={({ field }) => (
          <FormItem>
	            <FormLabel>{t("apiKeys.table.models")}</FormLabel>
            <FormControl>
              <Input {...field} disabled={providerManaged} placeholder="deepseek-v4-flash, local-coder" autoComplete="off" />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />

      {providerManaged && catalogModels.length > 0 ? <ul className="space-y-2 text-xs" aria-label={t("modelSources.subscription.catalogLimits")}>
        {catalogModels.map((model) => <li key={model.model} className="space-y-1 rounded-md border p-2">
          <p className="break-all font-medium">{model.model}</p>
          <p className="text-muted-foreground">{t("modelSources.fields.contextWindow")}: {model.contextWindow?.toLocaleString() ?? t("modelSources.subscription.usageUnknown")} · {t("modelSources.fields.maxOutputTokens")}: {model.maxOutputTokens?.toLocaleString() ?? t("modelSources.subscription.usageUnknown")}</p>
          <div className="flex flex-wrap gap-1">
            {model.supportsStreaming ? <Badge variant="outline">{t("modelSources.capabilities.streaming")}</Badge> : null}
            {model.supportsTools ? <Badge variant="outline">{t("modelSources.capabilities.tools")}</Badge> : null}
            {model.supportsVision ? <Badge variant="outline">{t("modelSources.capabilities.vision")}</Badge> : null}
          </div>
        </li>)}
      </ul> : null}

      {!metadataManaged ? (
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
	          <label className="text-sm font-medium">{t("modelSources.fields.contextWindow")}</label>
          <Input
            value={draft.contextWindow}
            onChange={(event) => updateDraft({ contextWindow: event.target.value })}
            placeholder="e.g. 32768"
            aria-label={t("modelSources.fields.contextWindow")}
            disabled={metadataManaged}
            inputMode="numeric"
          />
        </div>
        <div className="space-y-1">
	          <label className="text-sm font-medium">{t("modelSources.fields.maxOutputTokens")}</label>
          <Input
            value={draft.maxOutputTokens}
            onChange={(event) => updateDraft({ maxOutputTokens: event.target.value })}
            placeholder="e.g. 4096"
            aria-label={t("modelSources.fields.maxOutputTokens")}
            disabled={metadataManaged}
            inputMode="numeric"
          />
        </div>
      </div>
      ) : null}

      {!providerManaged || catalogModels.length > 0 ? <>
      <div className="space-y-2">
	        <div className="text-sm font-medium">{t("modelSources.fields.pricing")}</div>
	        <p className="text-xs text-muted-foreground">
	          {t("modelSources.fields.pricingDescription")}
	        </p>
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="space-y-1">
	            <label className="text-xs text-muted-foreground">{t("common.units.input")}</label>
            <Input
              value={draft.inputPer1M}
              onChange={(event) => updateDraft({ inputPer1M: event.target.value })}
              placeholder={t("modelSources.subscription.usageUnknown")}
              aria-label={t("common.units.input")}
              inputMode="decimal"
            />
          </div>
          <div className="space-y-1">
	            <label className="text-xs text-muted-foreground">{t("common.units.cached")}</label>
            <Input
              value={draft.cachedInputPer1M}
              onChange={(event) => updateDraft({ cachedInputPer1M: event.target.value })}
              placeholder={t("modelSources.subscription.usageUnknown")}
              aria-label={t("common.units.cached")}
              inputMode="decimal"
            />
          </div>
          <div className="space-y-1">
	            <label className="text-xs text-muted-foreground">{t("common.units.output")}</label>
            <Input
              value={draft.outputPer1M}
              onChange={(event) => updateDraft({ outputPer1M: event.target.value })}
              placeholder={t("modelSources.subscription.usageUnknown")}
              aria-label={t("common.units.output")}
              inputMode="decimal"
            />
          </div>
        </div>
      </div>

      <div className="space-y-2">
	        <div className="text-sm font-medium">{t("modelSources.fields.audioPricing")}</div>
	        <p className="text-xs text-muted-foreground">
	          {t("modelSources.fields.audioPricingDescription")}
	        </p>
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="space-y-1">
	            <label className="text-xs text-muted-foreground">{t("modelSources.fields.perMinute")}</label>
            <Input
              value={draft.audioPerMinute}
              onChange={(event) => updateDraft({ audioPerMinute: event.target.value })}
              placeholder={t("modelSources.subscription.usageUnknown")}
              aria-label={t("modelSources.fields.perMinute")}
              inputMode="decimal"
            />
          </div>
        </div>
      </div>
      </> : null}

      <div className="grid gap-2 sm:grid-cols-2">
        {(metadataManaged ? SOURCE_CAPABILITY_TOGGLES : CAPABILITY_TOGGLES).map(([key, labelKey]) => (
          <label key={key} className="flex items-center gap-2 rounded-md border p-2 text-sm">
            <Checkbox
              checked={draft[key]}
              disabled={providerManaged && (metadataManaged || SOURCE_CAPABILITY_KEYS[key] === true)}
              onCheckedChange={(checked) =>
                updateDraft({
                  [key]: checked === true,
                  ...(key === "supportsReasoning" && checked === true
                    ? {
                        reasoningEffortsInput:
                          draft.reasoningEffortsInput || DEFAULT_REASONING_EFFORTS.join(", "),
                        reasoningEfforts:
                          draft.reasoningEfforts.length > 0
                            ? draft.reasoningEfforts
                            : DEFAULT_REASONING_EFFORTS,
                        defaultReasoningEffort: draft.defaultReasoningEffort || "medium",
                      }
                    : {}),
                })
              }
            />
	            {t(labelKey)}
          </label>
        ))}
      </div>

      {draft.supportsReasoning && !metadataManaged ? (
        <div className="space-y-3 rounded-md border p-3">
          <div>
            <div className="text-sm font-medium">{t("modelSources.fields.reasoningEfforts")}</div>
            <p className="text-xs text-muted-foreground">
              {t("modelSources.fields.reasoningEffortsDescription")}
            </p>
          </div>

          <div className="space-y-1">
            <label className="text-xs text-muted-foreground" htmlFor="model-source-reasoning-efforts">
              {t("modelSources.fields.reasoningEfforts")}
            </label>
            <Input
              id="model-source-reasoning-efforts"
              disabled={metadataManaged}
              aria-label={t("modelSources.fields.reasoningEfforts")}
              value={draft.reasoningEffortsInput}
              onChange={(event) => {
                const reasoningEffortsInput = event.target.value;
                const reasoningEfforts = parseReasoningEffortsInput(reasoningEffortsInput);
                updateDraft({
                  reasoningEffortsInput,
                  reasoningEfforts,
                  defaultReasoningEffort: reasoningEfforts.includes(draft.defaultReasoningEffort)
                    ? draft.defaultReasoningEffort
                    : (reasoningEfforts[0] ?? ""),
                });
              }}
              placeholder={t("modelSources.fields.reasoningEffortsPlaceholder")}
              autoComplete="off"
            />
          </div>

          <div className="space-y-1">
            <label
              className="text-xs text-muted-foreground"
              htmlFor="model-source-default-reasoning-effort"
            >
              {t("modelSources.fields.defaultReasoningEffort")}
            </label>
            <Select
              value={draft.defaultReasoningEffort}
              onValueChange={(value) => updateDraft({ defaultReasoningEffort: value })}
              disabled={metadataManaged || draft.reasoningEfforts.length === 0}
            >
              <SelectTrigger
                id="model-source-default-reasoning-effort"
                aria-label={t("modelSources.fields.defaultReasoningEffort")}
                className="w-full"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {draft.reasoningEfforts.map((effort) => (
                  <SelectItem key={effort} value={effort}>
                    {effort}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
      ) : null}
      </> : null}
    </>
  );
}
