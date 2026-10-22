"use client";

import { useEffect, useState } from "react";
import { Card, Toggle } from "@/shared/components";
import { useTranslations } from "next-intl";
import { NumberField } from "./ResilienceFields";

type QuotaPreflightSettings = {
  enabled: boolean;
  defaultThresholdPercent: number;
  warnThresholdPercent: number;
  providerWindowDefaults: Record<string, Record<string, number>>;
};

/**
 * Quota Preflight Cutoff card (Settings → Routing).
 *
 * Surfaces the pre-existing resilience.quotaPreflight setting in the routing
 * UI: when enabled, credential selection runs a quota preflight per account
 * and skips accounts whose remaining quota (5h / weekly / monthly / credits —
 * whatever windows the provider's quota fetcher exposes) has dropped to the
 * configured cutoff, without sending the request upstream. Per-window values
 * set on the Provider Quota page (quotaWindowThresholds / providerWindowDefaults)
 * take precedence; the global default applies to everything else.
 */
export default function QuotaPreflightCard() {
  const t = useTranslations("settings");

  const [value, setValue] = useState<QuotaPreflightSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const resilienceRes = await fetch("/api/resilience", { cache: "no-store" });
        if (!resilienceRes.ok) throw new Error(`HTTP ${resilienceRes.status}`);
        const resilience = await resilienceRes.json();
        if (!alive) return;
        const qp = resilience.quotaPreflight;
        if (qp) {
          setValue({
            enabled: qp.enabled === true,
            defaultThresholdPercent: Number(qp.defaultThresholdPercent ?? 2),
            warnThresholdPercent: Number(qp.warnThresholdPercent ?? 20),
            providerWindowDefaults: qp.providerWindowDefaults || {},
          });
        }
      } catch {
        if (alive) setLoadFailed(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const save = async (patch: Partial<QuotaPreflightSettings>) => {
    if (!value) return;
    setSaving(true);
    try {
      const res = await fetch("/api/resilience", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ quotaPreflight: patch }),
      });
      const json = await res.json();
      if (!res.ok) {
        throw new Error(json?.error?.message || json?.error || `HTTP ${res.status}`);
      }
      const qp = json.quotaPreflight;
      if (qp) {
        setValue({
          enabled: qp.enabled === true,
          defaultThresholdPercent: Number(qp.defaultThresholdPercent ?? 2),
          warnThresholdPercent: Number(qp.warnThresholdPercent ?? 20),
          providerWindowDefaults: qp.providerWindowDefaults || {},
        });
      }
    } finally {
      setSaving(false);
    }
  };

  if (loadFailed) {
    return (
      <Card className="p-6">
        <p className="text-sm text-text-muted">
          {t("quotaPreflightLoadFailed")}
        </p>
      </Card>
    );
  }

  if (!value) return null;

  return (
    <Card>
      <div className="flex items-center gap-3 mb-4">
        <div className="p-2 rounded-lg bg-amber-500/10 text-amber-500 shrink-0">
          <span className="material-symbols-outlined text-[20px]" aria-hidden="true">
            battery_alert
          </span>
        </div>
        <div>
          <h3 className="text-base sm:text-lg font-semibold">
            {t("quotaPreflightTitle")}
          </h3>
          <p className="text-xs text-text-muted">
            {t("quotaPreflightSubtitle")}
          </p>
        </div>
      </div>

      <div className="flex flex-col gap-4">
        <div className="flex items-start sm:items-center justify-between gap-4">
          <div className="flex-1 min-w-0">
            <p className="font-medium text-sm sm:text-base">
              {t("quotaPreflightEnable")}
            </p>
            <p className="text-xs sm:text-sm text-text-muted">
              {t("quotaPreflightEnableDesc")}
            </p>
          </div>
          <Toggle
            checked={value.enabled}
            disabled={saving}
            onChange={() => save({ enabled: !value.enabled })}
          />
        </div>

        {value.enabled && (
          <>
            <div className="flex items-start sm:items-center justify-between gap-4 pt-2 border-t border-border/50">
              <div className="flex-1 min-w-0">
                <p className="font-medium text-sm sm:text-base">
                  {t("quotaPreflightCutoff")}
                </p>
                <p className="text-xs sm:text-sm text-text-muted">
                  {t("quotaPreflightCutoffDesc")}
                </p>
              </div>
              <div className="w-24 shrink-0">
                <NumberField
                  label=""
                  value={value.defaultThresholdPercent}
                  suffix="%"
                  min={0}
                  max={99}
                  onChange={(next) => {
                    if (next === value.defaultThresholdPercent) return;
                    save({ defaultThresholdPercent: next });
                  }}
                />
              </div>
            </div>
            <p className="text-xs text-text-muted">
              {t("quotaPreflightWindowsHint")}
            </p>
          </>
        )}
      </div>
    </Card>
  );
}
