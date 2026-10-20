import type { TFunction } from "i18next";

export function sourceWindowLabel(window: { windowKey: string; model: string | null }, t: TFunction): string {
  if (window.windowKey === "five_hour") {
    return window.model ? t("sourceMonitoring.windows.fiveHourModel", { model: window.model }) : t("sourceMonitoring.windows.fiveHour");
  }
  if (window.windowKey === "seven_day") {
    return window.model ? t("sourceMonitoring.windows.weeklyModel", { model: window.model }) : t("sourceMonitoring.windows.weekly");
  }
  if (window.windowKey.startsWith("seven_day_")) {
    return t("sourceMonitoring.windows.weeklyModel", { model: window.model ?? window.windowKey.slice("seven_day_".length) });
  }
  return window.model ? `${window.windowKey} · ${window.model}` : window.windowKey;
}

export function sourceWindowIsStale(window: { observedAt: string; resetsAt: string | null }, now: number): boolean {
  return now - Date.parse(window.observedAt) > 300_000 || (window.resetsAt !== null && Date.parse(window.resetsAt) <= now);
}
