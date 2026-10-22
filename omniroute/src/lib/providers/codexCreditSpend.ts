/**
 * Per-account Codex credit consumption limits.
 *
 * Codex reports a credit balance, never per-request credit charges. Consumption is
 * therefore observed as balance decreases between successive fresh observations;
 * increases (purchases, grants) are ignored rather than netted against spend. All
 * clients of the account contribute, not only this gateway. Spend before the first
 * observation is unknown, so a young tracker reports a lower bound.
 *
 * Limits are enforced before dispatch from the latest observation. Concurrent or
 * in-flight requests and observation lag can overshoot a limit; this is not an
 * atomic financial reservation.
 */
import { getDbInstance, isBuildPhase, isCloud } from "@/lib/db/core";

const NAMESPACE = "codexCreditSpend";
export const CODEX_CREDIT_DAY_MS = 24 * 60 * 60 * 1000;
export const CODEX_CREDIT_WEEK_MS = 7 * CODEX_CREDIT_DAY_MS;

interface CreditSpendState {
  lastBalance: number;
  lastObservedAt: number;
  trackedSince: number;
  drops: Array<{ at: number; amount: number }>;
}

export interface CodexCreditSpend {
  /** Observed consumption in the trailing 24 hours, in Codex credit units. */
  day: number;
  /** Observed consumption in the trailing 7 days, in Codex credit units. */
  week: number;
  /** First observation; windows that start earlier are lower bounds. */
  trackedSince: number;
}

export interface CodexCreditLimits {
  daily: number | null;
  weekly: number | null;
}

export interface CodexCreditLimitBreach {
  window: "daily" | "weekly";
  limit: number;
  spent: number;
}

type Db = {
  prepare: (sql: string) => {
    get: (...params: unknown[]) => { value?: string } | undefined;
    run: (...params: unknown[]) => unknown;
  };
  transaction: <T extends (...args: never[]) => unknown>(fn: T) => T;
};

function readState(db: Db, connectionId: string): CreditSpendState | null {
  const row = db.prepare("SELECT value FROM key_value WHERE namespace = ? AND key = ?")
    .get(NAMESPACE, connectionId);
  if (!row?.value) return null;
  try {
    const parsed = JSON.parse(row.value) as CreditSpendState;
    return Number.isFinite(parsed.lastBalance) && Number.isFinite(parsed.lastObservedAt) &&
      Number.isFinite(parsed.trackedSince) && Array.isArray(parsed.drops) ? parsed : null;
  } catch {
    return null;
  }
}

/** Record one fresh balance observation. Out-of-order or repeated observations are ignored. */
export function recordCodexCreditBalance(
  connectionId: string,
  balance: number | null | undefined,
  observedAt = Date.now()
): void {
  if (isBuildPhase || isCloud || !connectionId) return;
  if (typeof balance !== "number" || !Number.isFinite(balance) || balance < 0 ||
      !Number.isFinite(observedAt)) return;
  const db = getDbInstance() as unknown as Db;
  db.transaction(() => {
    const state = readState(db, connectionId);
    if (state && observedAt <= state.lastObservedAt) return;
    const drops = (state?.drops ?? []).filter((drop) => observedAt - drop.at < CODEX_CREDIT_WEEK_MS);
    if (state && balance < state.lastBalance) drops.push({ at: observedAt, amount: state.lastBalance - balance });
    const next: CreditSpendState = {
      lastBalance: balance,
      lastObservedAt: observedAt,
      trackedSince: state?.trackedSince ?? observedAt,
      drops,
    };
    db.prepare("INSERT OR REPLACE INTO key_value (namespace, key, value) VALUES (?, ?, ?)")
      .run(NAMESPACE, connectionId, JSON.stringify(next));
  })();
}

export function getCodexCreditSpend(connectionId: string, now = Date.now()): CodexCreditSpend | null {
  if (isBuildPhase || isCloud || !connectionId) return null;
  const state = readState(getDbInstance() as unknown as Db, connectionId);
  if (!state) return null;
  let day = 0;
  let week = 0;
  for (const drop of state.drops) {
    const age = now - drop.at;
    if (age < 0) continue;
    if (age < CODEX_CREDIT_WEEK_MS) week += drop.amount;
    if (age < CODEX_CREDIT_DAY_MS) day += drop.amount;
  }
  return { day, week, trackedSince: state.trackedSince };
}

function positiveLimit(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

export function readCodexCreditLimits(providerSpecificData: unknown): CodexCreditLimits {
  const data = providerSpecificData && typeof providerSpecificData === "object"
    ? providerSpecificData as Record<string, unknown> : {};
  return { daily: positiveLimit(data.creditDailyLimit), weekly: positiveLimit(data.creditWeeklyLimit) };
}

/**
 * The first exceeded window, or null when credit admission may proceed. Configured limits
 * without a spend observation fail closed: unknown consumption cannot authorize spend.
 */
export function findCodexCreditLimitBreach(
  limits: CodexCreditLimits,
  spend: CodexCreditSpend | null | undefined
): CodexCreditLimitBreach | null {
  if (limits.daily === null && limits.weekly === null) return null;
  const day = spend?.day ?? Number.POSITIVE_INFINITY;
  const week = spend?.week ?? Number.POSITIVE_INFINITY;
  if (limits.daily !== null && day >= limits.daily) return { window: "daily", limit: limits.daily, spent: day };
  if (limits.weekly !== null && week >= limits.weekly) return { window: "weekly", limit: limits.weekly, spent: week };
  return null;
}
