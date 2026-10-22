"use client";

import { useEffect, useState } from "react";

export interface ResetCreditMetadata {
  expiresAt: number | null;
  grantedAt: number | null;
}

type ResetCreditOverviewState =
  | { status: "loading" | "unavailable" }
  | {
      status: "loaded";
      credits: ResetCreditMetadata[];
      reportedCount: number | null;
      observedAt: number;
    };

function readTimestamp(value: unknown): number | null {
  if (typeof value !== "string" || !value.trim()) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

function readOverview(payload: unknown): ResetCreditOverviewState {
  if (!payload || typeof payload !== "object") return { status: "unavailable" };
  const data = payload as Record<string, unknown>;
  if (data.ok !== true || !Array.isArray(data.credits)) return { status: "unavailable" };
  const credits: ResetCreditMetadata[] = [];
  for (const value of data.credits) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return { status: "unavailable" };
    }
    const credit = value as Record<string, unknown>;
    // Keep dates only: redemption identifiers never enter overview state or markup.
    credits.push({
      expiresAt: readTimestamp(credit.expiresAt),
      grantedAt: readTimestamp(credit.grantedAt),
    });
  }
  credits.sort((a, b) => (a.expiresAt ?? Infinity) - (b.expiresAt ?? Infinity));
  const count = data.availableCount;
  return {
    status: "loaded",
    credits,
    reportedCount:
      typeof count === "number" && Number.isSafeInteger(count) && count >= credits.length
        ? count
        : null,
    observedAt: Date.now(),
  };
}

// The overview mounts with a connection-specific key. There is deliberately no shared cache:
// a late account response cannot overwrite another account or a newer mount.
export function useCodexResetCreditOverview(connectionId: string, enabled: boolean) {
  const [state, setState] = useState<ResetCreditOverviewState>({
    status: enabled ? "loading" : "unavailable",
  });
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch(
          `/api/usage/codex-reset-credit?connectionId=${encodeURIComponent(connectionId)}`,
          { method: "GET", cache: "no-store", signal: controller.signal }
        );
        const data: unknown = response.ok ? await response.json() : null;
        if (!controller.signal.aborted) {
          setState(response.ok ? readOverview(data) : { status: "unavailable" });
        }
      } catch {
        if (!controller.signal.aborted) setState({ status: "unavailable" });
      }
    }
    void load();
    return () => controller.abort();
  }, [connectionId, enabled]);
  return state;
}
