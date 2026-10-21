import { getProviderModels } from "@omniroute/open-sse/config/providerModels.ts";
import { isDiscoverableAntigravityModelId } from "@omniroute/open-sse/config/antigravityModelAliases.ts";
import { getResolvedModelCapabilities } from "@/lib/modelCapabilities";
import { getApiKeyMetadata, isModelAllowedForKey, validateApiKey } from "@/lib/db/apiKeys";
import { getProviderConnections } from "@/lib/db/providers";
import { getProviderLimitsCache, type ProviderLimitsCacheEntry } from "@/lib/db/providerLimits";
import { getSyncedAvailableModelsForConnection } from "@/lib/db/models";
import { resolveUsageAccountIdentity } from "@/lib/usage/accountIdentity";
import { getCodexQuotaWindowFilterForModel, isCodexReserveQuotaKey } from "@omniroute/open-sse/config/codexQuotaScopes.ts";
import { resolveQuotaKeyScope } from "@/lib/quota/quotaKey";
import { hasSelfAccountQuotaScope } from "@/shared/constants/selfServiceScopes";
import { blocksNativeSubscriptionOnlyKey } from "@/sse/handlers/chat/nativeSubscriptionRouting";

const OMP_PROVIDERS = {
  codex: { api: "openai-codex-responses", basePath: "/backend-api/codex" },
  claude: { api: "anthropic-messages", basePath: "/" },
  antigravity: { api: "openai-completions", basePath: "/v1" },
} as const;
const MAX_OBSERVATION_AGE_MS = 5 * 60_000;
const MAX_FUTURE_SKEW_MS = 30_000;
const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/;

export interface OmpKey {
  id: string;
  token: string;
  allowedConnections: string[];
  denyAllConnections?: boolean;
  forcedModel?: string | null;
  catalogScope?: string;
  scopes?: readonly string[];
}

export interface OmpConnection {
  id: string;
  provider: string;
  authType: string | null;
}

function isOmpConnection(row: Record<string, unknown>): row is Record<string, unknown> & OmpConnection {
  return typeof row.id === "string" && typeof row.provider === "string" &&
    (typeof row.authType === "string" || row.authType === null);
}

export interface OmpManifestModel {
  id: string;
  name: string;
  api: (typeof OMP_PROVIDERS)[keyof typeof OMP_PROVIDERS]["api"];
  basePath: string;
  reasoning: boolean;
  input: Array<"text" | "image">;
  contextWindow: number;
  maxTokens: number;
  thinking?: { mode: "effort"; efforts: string[] };
}

function isPositiveSafeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) > 0 && (value as number) <= 100_000_000;
}

export function bearerApiKey(request: Request): string | null {
  const authorization = request.headers.get("authorization");
  const match = authorization?.match(/^Bearer ([^\s]+)$/i);
  return match?.[1] ?? null;
}

/** This integration is API-key-only even when OmniRoute's general key gate is disabled. */
export async function authenticateOmpRequest(request: Request): Promise<OmpKey | null> {
  const key = bearerApiKey(request);
  if (!key || !(await validateApiKey(key))) return null;
  const metadata = await getApiKeyMetadata(key);
  if (!metadata || !metadata.isActive || metadata.isBanned || metadata.revokedAt) {
    return null;
  }
  let allowedConnections = metadata.allowedConnections ?? [];
  let denyAllConnections = false;
  if (metadata.allowedQuotas?.length) {
    const scope = await resolveQuotaKeyScope(metadata.allowedQuotas);
    allowedConnections = allowedConnections.length
      ? allowedConnections.filter((id) => scope.connectionIds.includes(id)) : scope.connectionIds;
    denyAllConnections = allowedConnections.length === 0;
  }
  return { id: metadata.id, token: key, allowedConnections, denyAllConnections,
    forcedModel: metadata.forcedModel, catalogScope: metadata.catalogScope, scopes: metadata.scopes };
}

export function connectionAllowed(key: OmpKey, connectionId: string): boolean {
  return !key.denyAllConnections && (key.allowedConnections.length === 0 || key.allowedConnections.includes(connectionId));
}


export function buildOmpManifest(
  connections: OmpConnection[],
  syncedModelsByConnection: ReadonlyMap<string, Array<{ id: string; name?: string; visibility?: string }>>,
  isAllowed: (provider: keyof typeof OMP_PROVIDERS, modelId: string) => boolean
): { schemaVersion: 1; models: OmpManifestModel[] } {
  const models: OmpManifestModel[] = [];
  const seen = new Set<string>();
  for (const provider of Object.keys(OMP_PROVIDERS) as Array<keyof typeof OMP_PROVIDERS>) {
    const eligible = connections.filter(
      (connection) => connection.provider === provider && connection.authType === "oauth"
    );
    if (!eligible.length) continue;
    const providerModels = new Map<string, { id: string; name?: string; visibility?: string }>();
    for (const connection of eligible) {
      for (const synced of syncedModelsByConnection.get(connection.id) ?? []) {
        if (synced.visibility === "hide" || !MODEL_ID.test(synced.id)) continue;
        if (provider === "antigravity" && !isDiscoverableAntigravityModelId(synced.id)) continue;
        providerModels.set(synced.id, synced);
      }
    }
    for (const [id, synced] of providerModels) {
      const publicId = provider === "antigravity" ? `${provider}/${id}` : id;
      if (!MODEL_ID.test(publicId) || seen.has(publicId) || !isAllowed(provider, id)) continue;
      const metadata = getProviderModels(provider).find((entry) => entry.id === id);
      // Reuse the catalog's persisted overrides and synced/spec metadata; missing limits stay absent.
      const resolved = provider === "antigravity" || !metadata
        ? getResolvedModelCapabilities({ provider, model: id }) : null;
      const contextWindow = resolved?.contextWindow ?? metadata?.contextLength;
      const maxTokens = resolved?.maxOutputTokens ?? metadata?.maxOutputTokens;
      if (!isPositiveSafeInteger(contextWindow) || !isPositiveSafeInteger(maxTokens)) continue;
      const reasoning = (resolved?.supportsThinking ?? metadata?.supportsReasoning) === true;
      const efforts = (resolved?.supportedThinkingEfforts ?? metadata?.supportedThinkingEfforts)?.filter((effort) =>
        ["minimal", "low", "medium", "high", "xhigh", "max"].includes(effort)
      );
      const declaredName = synced.name?.trim();
      const name = declaredName && declaredName.length <= 256 && !/[\x00-\x1f]/.test(declaredName)
        ? declaredName
        : metadata?.name ?? id;
      models.push({
        id: publicId,
        name,
        ...OMP_PROVIDERS[provider],
        reasoning,
        input: (resolved?.supportsVision ?? metadata?.supportsVision) ? ["text", "image"] : ["text"],
        contextWindow,
        maxTokens,
        ...(reasoning && efforts?.length ? { thinking: { mode: "effort", efforts: [...new Set(efforts)] } } : {}),
      });
      seen.add(publicId);
    }
  }
  return { schemaVersion: 1, models };
}

interface QuotaRecord {
  remaining?: unknown;
  total?: unknown;
  remainingPercentage?: unknown;
  fractionReported?: unknown;
  resetAt?: unknown;
  windowSeconds?: unknown;
  claudeQuota?: { kind?: unknown; modelId?: unknown };
}

export interface QuotaObservation {
  fetchedAt: string;
  quotas?: Record<string, unknown>;
  modelQuotas?: Record<string, unknown>;
}

export interface QuotaPool {
  connectionId: string;
  /** Verified quota principal, never exposed to clients. */
  capacityKey?: string;
  observation: QuotaObservation | null;
}

export interface OmpQuotaWindow {
  window: "5h" | "7d";
  remaining_account_equivalents: number | null;
  reporting_pools: number;
  unknown_pools: number;
  remaining_percent: number | null;
  observed_at: string | null;
  resets_at: string | null;
}

export interface OmpQuotaResponse {
  model: string;
  provider: "codex" | "claude" | null;
  generated_at: string;
  hidden: boolean;
  windows: OmpQuotaWindow[];
}

function finitePercentage(quota: QuotaRecord): number | null {
  if (quota.fractionReported === false) return null;
  const percentage = quota.remainingPercentage;
  if (typeof percentage === "number" && Number.isFinite(percentage) && percentage >= 0 && percentage <= 100) {
    return percentage;
  }
  const remaining = quota.remaining;
  const total = quota.total;
  if (typeof remaining === "number" && Number.isFinite(remaining) && remaining >= 0 &&
      typeof total === "number" && Number.isFinite(total) && total > 0 && remaining <= total) {
    return (remaining / total) * 100;
  }
  return null;
}

function validTimestamp(value: unknown, now: number): value is string {
  if (typeof value !== "string") return false;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) && now - timestamp >= -MAX_FUTURE_SKEW_MS && now - timestamp <= MAX_OBSERVATION_AGE_MS;
}

function quotaForExactModel(
  observation: QuotaObservation | null,
  model: string,
  provider: "codex" | "claude",
  now: number
): Array<{ window: "5h" | "7d"; quota: QuotaRecord; observedAt: string }> {
  if (!observation || !validTimestamp(observation.fetchedAt, now)) return [];
  const result: Array<{ window: "5h" | "7d"; quota: QuotaRecord; observedAt: string }> = [];
  const codexWindowFilter = provider === "codex" ? getCodexQuotaWindowFilterForModel(model) : undefined;
  const append = (key: string, value: unknown, exactModelOnly: boolean) => {
    if (provider === "codex" && !exactModelOnly &&
        (isCodexReserveQuotaKey(key) || (codexWindowFilter && !codexWindowFilter(key)))) return;
    if (!value || typeof value !== "object" || Array.isArray(value)) return;
    const quota = value as QuotaRecord;
    const modelMatches = key === model || quota.claudeQuota?.modelId === model;
    if (exactModelOnly && !modelMatches) return;
    let window: "5h" | "7d" | null = null;
    if (provider === "claude") {
      const kind = quota.claudeQuota?.kind;
      if (kind === "session") window = "5h";
      else if (kind === "weekly_all" || (kind === "weekly_scoped" && modelMatches)) window = "7d";
      else if (!kind && /(?:^|[^a-z0-9])(?:5h|session)(?:$|[^a-z0-9])/i.test(key)) window = "5h";
      else if (!kind && /(?:^|[^a-z0-9])(?:7d|weekly)(?:$|[^a-z0-9])/i.test(key)) window = "7d";
    } else {
      const seconds = quota.windowSeconds;
      if (typeof seconds === "number" && Number.isFinite(seconds)) {
        if (seconds > 0 && seconds <= 6 * 60 * 60) window = "5h";
        else if (seconds >= 6 * 24 * 60 * 60) window = "7d";
      }
    }
    if (window) result.push({ window, quota, observedAt: observation.fetchedAt });
  };
  for (const [key, value] of Object.entries(observation.quotas ?? {})) append(key, value, false);
  for (const [key, value] of Object.entries(observation.modelQuotas ?? {})) append(key, value, true);
  return result;
}

export function buildOmpQuota(
  model: string,
  provider: "codex" | "claude" | null,
  pools: QuotaPool[],
  now = Date.now()
): OmpQuotaResponse {
  const generatedAt = new Date(now).toISOString();
  if (!MODEL_ID.test(model) || !provider) {
    return { model, provider: null, generated_at: generatedAt, hidden: false, windows: [] };
  }
  const observations = new Map<"5h" | "7d", Array<{ percent: number; observedAt: string; resetAt: string | null }>>();
  const unknown = new Map<"5h" | "7d", number>();
  for (const window of ["5h", "7d"] as const) {
    observations.set(window, []);
    unknown.set(window, 0);
  }
  const capacityPools = new Map<string, QuotaPool[]>();
  for (const pool of pools) {
    const key = pool.capacityKey ?? pool.connectionId;
    const peers = capacityPools.get(key) ?? [];
    peers.push(pool);
    capacityPools.set(key, peers);
  }
  for (const peers of capacityPools.values()) {
    const matches = peers.flatMap((pool) => quotaForExactModel(pool.observation, model, provider, now));
    for (const window of ["5h", "7d"] as const) {
      const entries = matches.filter((entry) => entry.window === window);
      const valid = entries.flatMap((entry) => {
        const percentage = finitePercentage(entry.quota);
        const resetTime = typeof entry.quota.resetAt === "string" ? Date.parse(entry.quota.resetAt) : NaN;
        if (Number.isFinite(resetTime) && resetTime <= now) return [];
        const resetAt = Number.isFinite(resetTime) ? entry.quota.resetAt as string : null;
        return percentage === null ? [] : [{ percent: percentage, observedAt: entry.observedAt, resetAt }];
      });
      if (!valid.length) {
        unknown.set(window, (unknown.get(window) ?? 0) + 1);
        continue;
      }
      const conservative = valid.reduce((lowest, current) => current.percent < lowest.percent ? current : lowest);
      observations.get(window)?.push(conservative);
    }
  }
  const windows = (["5h", "7d"] as const).map((window): OmpQuotaWindow => {
    const known = observations.get(window) ?? [];
    const remaining = known.reduce((sum, item) => sum + item.percent / 100, 0);
    return {
      window,
      remaining_account_equivalents: known.length ? remaining : null,
      reporting_pools: known.length,
      unknown_pools: unknown.get(window) ?? 0,
      remaining_percent: known.length && !unknown.get(window) ? remaining / known.length * 100 : null,
      observed_at: known.length ? known.map((item) => item.observedAt).sort()[0] : null,
      resets_at: known.length ? known.map((item) => item.resetAt).filter((item): item is string => item !== null).sort()[0] ?? null : null,
    };
  });
  return { model, provider, generated_at: generatedAt, hidden: false, windows };
}

function isModelProvider(provider: string): provider is keyof typeof OMP_PROVIDERS {
  return Object.hasOwn(OMP_PROVIDERS, provider);
}


export async function getOmpManifestAuthorized(key: OmpKey) {
  const connections = (await Promise.all(
    Object.keys(OMP_PROVIDERS).map(async (provider) => {
      if (await blocksNativeSubscriptionOnlyKey(key.id, provider)) return [];
      return getProviderConnections(
        { provider, isActive: true }, undefined, undefined,
        ["id", "provider", "auth_type", "is_active"]
      );
    })
  )).flat().filter(isOmpConnection).filter((connection) =>
    isModelProvider(connection.provider) && connection.authType === "oauth" && connectionAllowed(key, connection.id)
  );
  const synced = new Map<string, Array<{ id: string; name: string; visibility?: string }>>();
  await Promise.all(connections.map(async (connection) => {
    synced.set(connection.id, await getSyncedAvailableModelsForConnection(connection.provider, connection.id));
  }));
  const manifestCandidates = buildOmpManifest(connections, synced, () => true);
  const permitted = new Set<string>();
  await Promise.all(manifestCandidates.models.map(async (model) => {
    const provider = model.api === "openai-completions" ? "antigravity"
      : model.api === "anthropic-messages" ? "claude" : "codex";
    const rawId = provider === "antigravity" ? model.id.slice(provider.length + 1) : model.id;
    const qualifiedId = `${provider}/${rawId}`;
    // A bare Gemini pin can resolve to a different provider after chat rewrites body.model.
    if (key.catalogScope === "combos" || (key.forcedModel &&
        key.forcedModel !== qualifiedId && (provider === "antigravity" || key.forcedModel !== rawId))) return;
    const allowed = provider === "antigravity"
      ? await isModelAllowedForKey(key.token, qualifiedId)
      : await isModelAllowedForKey(key.token, qualifiedId, rawId);
    if (allowed) permitted.add(model.id);
  }));
  return {
    schemaVersion: 1 as const,
    models: manifestCandidates.models.filter((model) => permitted.has(model.id)),
  };
}

function quotaCapacityIdentity(connection: Record<string, unknown>): string {
  const raw = connection.providerSpecificData;
  const data = raw && typeof raw === "object" && !Array.isArray(raw)
    ? raw as Record<string, unknown> : {};
  if (connection.provider === "claude" && typeof data.accountUUID === "string" &&
      data.accountUUID && typeof data.organizationUUID === "string" && data.organizationUUID) {
    return JSON.stringify(["claude", data.accountUUID, data.organizationUUID]);
  }
  return resolveUsageAccountIdentity(connection).accountKey;
}

export async function getOmpQuotaForKey(key: OmpKey, model: string): Promise<OmpQuotaResponse> {
  if (!hasSelfAccountQuotaScope(key.scopes)) {
    return { model, provider: null, generated_at: new Date().toISOString(), hidden: true, windows: [] };
  }
  if (!MODEL_ID.test(model)) return buildOmpQuota(model, null, [], Date.now());
  const manifest = await getOmpManifestAuthorized(key);
  const selected = manifest.models.find((entry) => entry.id === model);
  if (!selected || selected.api === "openai-completions") return buildOmpQuota(model, null, [], Date.now());
  const provider = selected.api === "anthropic-messages" ? "claude" : "codex";
  const connections = await getProviderConnections(
    { provider, isActive: true }, undefined, undefined,
    ["id", "provider", "auth_type", "is_active", "email", "provider_specific_data"]
  );
  const modelConnections = await Promise.all(connections.filter(isOmpConnection).map(async (connection) => ({
    connection,
    hasModel: (await getSyncedAvailableModelsForConnection(provider, connection.id)).some(
      (entry) => entry.id === model && entry.visibility !== "hide"
    ),
  })));
  const authorized = modelConnections
    .filter(({ connection, hasModel }) =>
      hasModel && connection.authType === "oauth" && connectionAllowed(key, connection.id)
    )
    .map(({ connection }) => connection);
  const pools = authorized.map((connection) => {
    const cache: ProviderLimitsCacheEntry | null = getProviderLimitsCache(connection.id);
    return {
      connectionId: connection.id,
      capacityKey: quotaCapacityIdentity(connection),
      observation: cache
        ? { fetchedAt: cache.fetchedAt, quotas: cache.quotas ?? undefined, modelQuotas: cache.modelQuotas }
        : null,
    };
  });
  return buildOmpQuota(model, provider, pools);
}
