"use client";

import { useTranslations } from "next-intl";
import { Toggle } from "@/shared/components";

type ClaudeConnectionFieldsProps = {
  values: {
    blockExtraUsage: boolean;
    lowPriorityMode: boolean;
    autoLimitReset: boolean;
  };
  /** The usage-wall options only exist for subscription (OAuth) connections. */
  showUsageWallOptions: boolean;
  onChange: (patch: Partial<ClaudeConnectionFieldsProps["values"]>) => void;
};

/** The positive credit permission is stored as the inverse blockExtraUsage flag. */
export default function ClaudeConnectionFields(props: ClaudeConnectionFieldsProps) {
  const t = useTranslations("providers");

  return (
    <div className="flex flex-col gap-4 rounded-lg border border-border/50 bg-surface/20 p-4">
      {props.showUsageWallOptions && (
        <div>
          <Toggle
            checked={!props.values.blockExtraUsage}
            onChange={(checked) => props.onChange({ blockExtraUsage: !checked })}
            label={t("subscriptionCreditAutoUseLabel")}
            description={t("subscriptionCreditAutoUseDescription")}
          />
          <p className="mt-2 text-xs text-text-muted">{t("subscriptionCreditBoundary")}</p>
          <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">
            {t("subscriptionCreditRisk")}
          </p>
        </div>
      )}
      {props.showUsageWallOptions && (
        <>
          <Toggle
            checked={props.values.lowPriorityMode}
            onChange={(checked) => props.onChange({ lowPriorityMode: checked })}
            label={t("claudeLowPriorityModeLabel")}
            description={t("claudeLowPriorityModeDescription")}
          />
          <Toggle
            checked={props.values.autoLimitReset}
            onChange={(checked) => props.onChange({ autoLimitReset: checked })}
            label={t("claudeAutoLimitResetLabel")}
            description={t("claudeAutoLimitResetDescription")}
          />
        </>
      )}
    </div>
  );
}
