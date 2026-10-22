"use client";

import { useCallback, useEffect, useState } from "react";
import { Input, Select } from "@/shared/components";
import { useTranslations } from "next-intl";
import { ACCOUNT_FALLBACK_STRATEGY_VALUES } from "@/shared/constants/routingStrategies";
import { toNumber } from "@/shared/utils/numeric";

type ProviderStrategyOverride = {
  fallbackStrategy?: string;
  stickyRoundRobinLimit?: number;
};

type RoutingSettings = {
  fallbackStrategy?: string;
  stickyRoundRobinLimit?: number;
  providerStrategies?: Record<string, ProviderStrategyOverride>;
};

type Props = {
  providerKey: string;
  connectionCount: number;
};

const STRATEGY_OPTIONS = ACCOUNT_FALLBACK_STRATEGY_VALUES.filter((v) =>
  ["fill-first", "round-robin", "priority", "p2c", "random", "least-used", "expiry-first"].includes(
    v
  )
);

function clampProviderStickyLimit(raw: string): number {
  const val = parseInt(raw, 10);
  return Math.min(1000, Math.max(1, Number.isNaN(val) ? 3 : val));
}

async function fetchRoutingSettings(): Promise<RoutingSettings | null> {
  const res = await fetch("/api/settings", { cache: "no-store" });
  if (!res.ok) return null;
  return res.json();
}

/** Loads/saves the per-provider account-routing override. Extracted out of the
 * component body to keep ProviderAccountRoutingCard's own render function small. */
function useProviderAccountRoutingState(providerKey: string) {
  const [strategy, setStrategy] = useState<string>("");
  const [stickyLimit, setStickyLimit] = useState("");
  const [settings, setSettings] = useState<RoutingSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const applySettings = useCallback((data: RoutingSettings) => {
    const override = data.providerStrategies?.[providerKey];
    setSettings(data);
    setStrategy(override?.fallbackStrategy || "");
    setStickyLimit(
      override?.stickyRoundRobinLimit != null ? String(override.stickyRoundRobinLimit) : ""
    );
  }, [providerKey]);

  const load = useCallback(async () => {
    const result = await fetchRoutingSettings();
    if (!result) throw new Error("Failed to refresh current settings");
    applySettings(result);
  }, [applySettings]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const result = await fetchRoutingSettings();
      if (cancelled) return;
      if (result) applySettings(result);
      else setError("providerRoutingLoadError");
    })().catch((e) => {
      if (!cancelled) setError("providerRoutingLoadError");
      console.error(e);
    });
    return () => {
      cancelled = true;
    };
  }, [applySettings]);

  const save = useCallback(
    async (nextStrategy: string, nextSticky: string) => {
      setBusy(true);
      setError(null);
      try {
        const res = await fetch("/api/settings", { cache: "no-store" });
        if (!res.ok) throw new Error("Failed to fetch current settings");
        const data = await res.json();
        const revision = typeof data.settingsRevision === "number" ? data.settingsRevision : 0;
        const current = (data?.providerStrategies || {}) as Record<
          string,
          ProviderStrategyOverride
        >;
        const override: ProviderStrategyOverride = {};
        if (nextStrategy) override.fallbackStrategy = nextStrategy;
        if (nextStrategy === "round-robin" && nextSticky !== "") {
          override.stickyRoundRobinLimit = clampProviderStickyLimit(nextSticky);
        }
        const updated = { ...current };
        if (Object.keys(override).length === 0) delete updated[providerKey];
        else updated[providerKey] = override;
        const patchRes = await fetch("/api/settings", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ providerStrategies: updated, expectedRevision: revision }),
        });
        if (patchRes.status === 409) {
          await load();
          setError("providerRoutingConflict");
          throw new Error("Settings conflict — refreshed from server");
        }
        if (!patchRes.ok) throw new Error("Failed to save provider routing settings");
        applySettings({ ...data, providerStrategies: updated });
      } catch (e) {
        console.error(e);
        setError((current) => current || "providerRoutingSaveError");
      } finally {
        setBusy(false);
      }
    },
    [applySettings, load, providerKey]
  );

  const globalStrategy = settings ? settings.fallbackStrategy || "fill-first" : null;
  const globalStickyLimit = settings ? toNumber(settings.stickyRoundRobinLimit, 3) : null;
  const appliedProviderStrategy = settings?.providerStrategies?.[providerKey]?.fallbackStrategy;
  const effectiveStrategy = appliedProviderStrategy || globalStrategy;
  const providerStickyLimit = settings?.providerStrategies?.[providerKey]?.stickyRoundRobinLimit;
  const effectiveStickyLimit = settings
    ? toNumber(providerStickyLimit ?? settings.stickyRoundRobinLimit, 3)
    : null;

  return {
    strategy, setStrategy, stickyLimit, setStickyLimit, busy, save, error,
    loaded: settings !== null, globalStrategy, globalStickyLimit,
    effectiveStrategy, effectiveStickyLimit, hasProviderStickyLimit: providerStickyLimit != null,
    hasProviderStrategy: Boolean(appliedProviderStrategy),
  };
}

function ProviderAccountRoutingCardContent({ providerKey, connectionCount }: Props) {
  const t = useTranslations("settings");
  const {
    strategy, setStrategy, stickyLimit, setStickyLimit, busy, save, error, loaded,
    globalStrategy, globalStickyLimit, effectiveStrategy, effectiveStickyLimit, hasProviderStickyLimit,
    hasProviderStrategy,
  } = useProviderAccountRoutingState(providerKey);
  const strategyLabel = (value: string) =>
    t.has(`accountStrategyGuide.${value}.name`)
      ? `${t(`accountStrategyGuide.${value}.name`)} (${value})`
      : value;
  const hasGuide = effectiveStrategy && t.has(`accountStrategyGuide.${effectiveStrategy}.summary`);

  if (connectionCount < 2) return null;

  return (
    <div className="mb-4 min-w-0 rounded-lg border border-border/60 bg-black/[0.02] dark:bg-white/[0.02] p-3">
      <p className="text-sm font-medium">{t("providerAccountRoutingTitle")}</p>
      <p className="text-xs text-text-muted mb-3">{t("providerAccountRoutingDesc")}</p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Select
          className="min-w-0"
          label={t("providerRoutingStrategy")}
          disabled={busy || !loaded}
          value={strategy}
          onChange={(e) => {
            const v = e.target.value;
            setStrategy(v);
            save(v, stickyLimit).catch(console.error);
          }}
        >
          <option value="">
            {t("providerRoutingInheritGlobal")}
            {globalStrategy ? ` — ${strategyLabel(globalStrategy)}` : ""}
          </option>
          {strategy && !STRATEGY_OPTIONS.includes(strategy as (typeof STRATEGY_OPTIONS)[number]) && (
            <option value={strategy}>{strategyLabel(strategy)}</option>
          )}
          {STRATEGY_OPTIONS.map((opt) => (
            <option key={opt} value={opt}>
              {strategyLabel(opt)}
            </option>
          ))}
        </Select>
        {loaded && effectiveStrategy === "round-robin" && (
          <Input
            className="min-w-0"
            label={t("providerRoutingStickyLabel")}
            type="number"
            min={1}
            max={1000}
            disabled={busy || !strategy}
            value={stickyLimit}
            placeholder={globalStickyLimit != null ? String(globalStickyLimit) : ""}
            hint={t(!hasProviderStickyLimit
              ? "providerRoutingStickyInherited"
              : "providerRoutingStickyOverride", { count: effectiveStickyLimit ?? 3 })}
            onChange={(e) => setStickyLimit(e.target.value)}
            onBlur={() => save(strategy, stickyLimit).catch(console.error)}
          />
        )}
      </div>
      <div className="mt-3 space-y-2 text-xs leading-relaxed" aria-live="polite">
        {loaded && effectiveStrategy ? (
          <>
            <p className="font-medium text-text-main">
              {t(hasProviderStrategy ? "providerRoutingEffectiveOverride" : "providerRoutingEffectiveGlobal", {
                strategy: strategyLabel(effectiveStrategy),
              })}
            </p>
            {hasGuide && (
              <>
                <p className="text-text-main">{t(`accountStrategyGuide.${effectiveStrategy}.summary`)}</p>
                <p className="text-text-muted">
                  {t("providerRoutingRecommended")} {t(`accountStrategyGuide.${effectiveStrategy}.use`)}
                </p>
                <p className="text-text-muted">
                  {t("providerRoutingCaveat")} {t(`accountStrategyGuide.${effectiveStrategy}.caveat`)}
                </p>
              </>
            )}
          </>
        ) : (
          <p className="text-text-muted">{t("providerRoutingEffectiveUnknown")}</p>
        )}
        <p className="text-text-muted">{t("providerRoutingOrderingLimits")}</p>
        {error && <p role="alert" className="text-red-500">{t(error)}</p>}
      </div>
      <details className="mt-3 border-t border-border/60 pt-3">
        <summary className="cursor-pointer text-xs font-medium text-text-main focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">
          {t("providerRoutingCompare")}
        </summary>
        <dl className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          {STRATEGY_OPTIONS.map((opt) => (
            <div key={opt} className="min-w-0 space-y-1 text-xs leading-relaxed">
              <dt className="font-medium text-text-main">{strategyLabel(opt)}</dt>
              <dd className="text-text-muted">{t(`accountStrategyGuide.${opt}.summary`)}</dd>
              <dd className="text-text-muted">
                {t("providerRoutingRecommended")} {t(`accountStrategyGuide.${opt}.use`)}
              </dd>
              <dd className="text-text-muted">
                {t("providerRoutingCaveat")} {t(`accountStrategyGuide.${opt}.caveat`)}
              </dd>
            </div>
          ))}
        </dl>
      </details>
    </div>
  );
}

export default function ProviderAccountRoutingCard(props: Props) {
  return <ProviderAccountRoutingCardContent key={props.providerKey} {...props} />;
}
