import { NextResponse } from "next/server";
import {
  getLastProviderLimitsAutoSyncTime,
  getProviderLimitsSyncIntervalMinutes,
  getSanitizedCachedProviderLimitsMap,
  syncAllProviderLimits,
} from "@/lib/usage/providerLimits";
import { attachConnectionSpeedStats } from "@/lib/usage/connectionSpeedStats";
import { getCodexCreditSpend } from "@/lib/providers/codexCreditSpend";

/** Observed rolling Codex credit consumption beside each reported balance (both endpoints). */
function attachCodexCreditSpend<T extends Record<string, unknown>>(caches: Record<string, T>) {
  return Object.fromEntries(Object.entries(caches).map(([connectionId, entry]) => {
    const spend = entry && entry.paidCredits ? getCodexCreditSpend(connectionId) : null;
    return [connectionId, spend ? { ...entry, creditSpend: spend } : entry];
  }));
}

/**
 * GET /api/usage/provider-limits
 * Returns cached Provider Limits data without triggering live refreshes. Each entry
 * also carries its connection's read-time `speed` stats when available.
 */
export async function GET() {
  try {
    return NextResponse.json({
      caches: attachCodexCreditSpend(attachConnectionSpeedStats(await getSanitizedCachedProviderLimitsMap())),
      intervalMinutes: getProviderLimitsSyncIntervalMinutes(),
      lastAutoSyncAt: await getLastProviderLimitsAutoSyncTime(),
    });
  } catch (error) {
    console.error("[API] GET /api/usage/provider-limits error:", error);
    return NextResponse.json({ error: "Failed to fetch cached provider limits" }, { status: 500 });
  }
}

/**
 * POST /api/usage/provider-limits
 * Manually refresh all supported Provider Limits entries.
 */
export async function POST() {
  try {
    const result = await syncAllProviderLimits({ source: "manual" });
    const caches = attachCodexCreditSpend(attachConnectionSpeedStats(await getSanitizedCachedProviderLimitsMap()));
    return NextResponse.json({
      ...result,
      caches,
      intervalMinutes: getProviderLimitsSyncIntervalMinutes(),
      lastAutoSyncAt: await getLastProviderLimitsAutoSyncTime(),
    });
  } catch (error) {
    console.error("[API] POST /api/usage/provider-limits error:", error);
    return NextResponse.json({ error: "Failed to refresh provider limits" }, { status: 500 });
  }
}
