import { useTranslation } from "react-i18next";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { BILLING_POLICIES, type BillingPolicy } from "@/features/api-keys/schemas";

type BillingPolicySelectProps = {
  id: string;
  value: BillingPolicy;
  disabled: boolean;
  onChange: (value: BillingPolicy) => void;
};

export function BillingPolicySelect({ id, value, disabled, onChange }: BillingPolicySelectProps) {
  const { t } = useTranslation();
  const descriptionId = `${id}-description`;

  return (
    <div className="space-y-1">
      <label htmlFor={id} className="text-sm font-medium">
        {t("apiKeys.billingPolicy.label")}
      </label>
      <Select value={value} disabled={disabled} onValueChange={(next) => onChange(next as BillingPolicy)}>
        <SelectTrigger id={id} aria-describedby={descriptionId}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {BILLING_POLICIES.map((policy) => (
            <SelectItem key={policy} value={policy}>
              {t(`apiKeys.billingPolicy.${policy}`)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <p id={descriptionId} className="text-xs text-muted-foreground">
        {t("apiKeys.billingPolicy.description")}
      </p>
    </div>
  );
}
