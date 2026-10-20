import { useId } from "react";
import { useTranslation } from "react-i18next";

import { Input } from "@/components/ui/input";
import { useModels } from "@/features/api-keys/hooks/use-models";
import { useSourceAccess } from "@/features/api-keys/hooks/use-source-access";

export function EnforcedModelInput({ id, value, onChange }: { id?: string; value: string; onChange: (value: string) => void }) {
  const { t } = useTranslation();
  const listId = useId();
  const { data: models = [] } = useModels();
  const sourceAccess = useSourceAccess();
  const modelIds = [...new Set([...models.map((model) => model.id), ...(sourceAccess.data?.models.map((model) => model.model) ?? [])])];
  return (
    <>
      <Input id={id} aria-label={t("apiKeys.form.enforcedModel")} value={value} onChange={(event) => onChange(event.target.value)} list={listId} autoComplete="off" placeholder={t("apiKeyMonitoring.modelPlaceholder")} />
      <datalist id={listId}>
        {modelIds.map((model) => {
          const sources = sourceAccess.data?.models.filter((entry) => entry.model === model) ?? [];
          const availability = sources.some((entry) => entry.availability === "available") ? "available" : sources.some((entry) => entry.availability === "quota_exhausted") ? "quota_exhausted" : "unavailable";
          return <option key={model} value={model} label={sources.length > 0 ? `${model} · ${t(`apiKeyMonitoring.availability.${availability}`)}` : model} />;
        })}
      </datalist>
    </>
  );
}
