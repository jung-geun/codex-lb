import { getDbInstance } from "./core";
import { quotaCapacityIdentity } from "@/lib/quota/capacityIdentity";

interface RawConnection {
  id: string;
  provider: string;
  provider_specific_data: string | null;
}

interface RawSnapshot {
  window_key: string;
  remaining_percentage: number | null;
  next_reset_at: string | null;
  created_at: string;
  raw_data: string | null;
}

interface RawRequest {
  timestamp: string;
  duration: number | null;
  model: string | null;
  status: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  usage_provenance: string | null;
}

export interface QuotaCapacityEvidence {
  connection: { id: string; provider: string; identity: string | null } | null;
  snapshots: Array<{
    windowKey: string;
    remainingPercentage: number | null;
    resetsAt: string | null;
    createdAt: string;
    rawData: string | null;
    unsupportedScope: boolean;
    identityMatches: boolean;
  }>;
  requests: Array<{
    startedAt: string;
    durationMs: number;
    model: string | null;
    inputTokens: number | null;
    outputTokens: number | null;
    success: boolean;
    reported: boolean;
  }>;
}

const GLOBAL_WINDOW_KEYS = new Set([
  "session",
  "weekly",
  "monthly",
  "daily",
  "primary",
  "secondary",
  "five_hour",
  "seven_day",
  "dailyinputtokens",
  "weeklyinputtokens",
  "monthlyinputtokens",
  "tokens",
  "session (5h)",
  "weekly (7d)",
]);

function isKnownGlobalWindow(windowKey: string): boolean {
  return GLOBAL_WINDOW_KEYS.has(windowKey.trim().toLowerCase());
}

function isQuotaFractionUnreported(rawData: string | null): boolean {
  if (!rawData) return false;
  try {
    return (JSON.parse(rawData) as Record<string, unknown>).fractionReported === false;
  } catch {
    return true;
  }
}

/** Reads only exact-connection snapshots and confirmed request logs; no writes or credentials. */
export function getQuotaCapacityEvidence(
  connectionId: string,
  since: string,
  until: string
): QuotaCapacityEvidence {
  const db = getDbInstance();
  const connection = db
    .prepare("SELECT id, provider, provider_specific_data FROM provider_connections WHERE id = ?")
    .get(connectionId) as RawConnection | undefined;
  if (!connection) return { connection: null, snapshots: [], requests: [] };
  const identity = quotaCapacityIdentity(connection.provider, connection.provider_specific_data);

  const snapshots = db
    .prepare(
      `SELECT window_key, remaining_percentage, next_reset_at, created_at, raw_data
       FROM quota_snapshots
       WHERE connection_id = ? AND provider = ? AND created_at >= ? AND created_at <= ?
       ORDER BY created_at ASC, id ASC`
    )
    .all(connectionId, connection.provider, since, until) as RawSnapshot[];
  const requests = db
    .prepare(
      `SELECT timestamp, duration, model, status, tokens_in, tokens_out, usage_provenance
       FROM call_logs
       WHERE connection_id = ? AND provider = ? AND timestamp >= ? AND timestamp <= ?
       ORDER BY timestamp ASC, id ASC`
    )
    .all(connectionId, connection.provider, since, until) as RawRequest[];

  const isUnsupportedScope = (row: RawSnapshot) => {
    if (/model|spark|\b(?:opus|sonnet|haiku)\b/i.test(row.window_key)) return true;
    if (!isKnownGlobalWindow(row.window_key)) return true;
    if (!row.raw_data) return false;
    try {
      const data = JSON.parse(row.raw_data) as Record<string, unknown>;
      return ["model", "modelId", "model_id", "modelKey", "model_key"].some(
        (key) => typeof data[key] === "string" && data[key] !== ""
      );
    } catch {
      return true;
    }
  };

  return {
    connection: { id: connection.id, provider: connection.provider, identity },
    snapshots: snapshots.map((row) => ({
      windowKey: row.window_key,
      remainingPercentage:
        isQuotaFractionUnreported(row.raw_data) ||
        typeof row.remaining_percentage !== "number" ||
        !Number.isFinite(row.remaining_percentage) ||
        row.remaining_percentage < 0 ||
        row.remaining_percentage > 100
          ? null
          : row.remaining_percentage,
      resetsAt: row.next_reset_at,
      createdAt: row.created_at,
      unsupportedScope: isUnsupportedScope(row),
      identityMatches: identity !== null && (() => {
        try { return JSON.parse(row.raw_data ?? "{}").capacityIdentity === identity; } catch { return false; }
      })(),
      rawData: row.raw_data,
    })),
    requests: requests.map((row) => ({
      startedAt: row.timestamp,
      durationMs: typeof row.duration === "number" ? row.duration : 0,
      model: row.model,
      inputTokens:
        typeof row.tokens_in === "number" && Number.isSafeInteger(row.tokens_in) ? row.tokens_in : null,
      outputTokens:
        typeof row.tokens_out === "number" && Number.isSafeInteger(row.tokens_out) ? row.tokens_out : null,
      success: typeof row.status === "number" && row.status >= 200 && row.status < 300,
      reported: row.usage_provenance === "reported",
    })),
  };
}
