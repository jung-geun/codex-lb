import { getProviderConnections } from "@/lib/db/providers";
import { getSyncedAvailableModelsForConnection } from "@/lib/db/models";
import { getCachedSettings } from "@/lib/db/readCache";
import { isModelAllowedForKey } from "@/lib/db/apiKeys";
import { selectNativePaidFallbacks, markNativeResponseTerminal } from "@omniroute/open-sse/services/combo/nativeSubscriptionFallback.ts";
import { classifyConnectionBilling, isOverageSafe } from "@omniroute/open-sse/services/autoCombo/connectionBilling.ts";
import { resolveQuotaKeyScope } from "@/lib/quota/quotaKey";
import { hasNativeCodexTurnBinding, hasCodexIdentityHeaders } from "@omniroute/open-sse/config/codexIdentity.ts";
import { isClaudeExtraUsageAllowed } from "@/lib/providers/claudeExtraUsage";
import { isCodexPaidCreditsEnabled } from "@/lib/providers/codexPaidCredits";

const safeRefusals = new WeakSet<Response>();

function isRoutingConnection(row: Record<string, unknown>): row is Record<string, unknown> & {
  id: string; provider: string; authType: string;
} {
  return typeof row.id === "string" && typeof row.provider === "string" && typeof row.authType === "string";
}

/** Internal evidence only: never trust a client-supplied response header. */
export function markNativePreDispatchRefusal(response: Response): Response {
  safeRefusals.add(response);
  return response;
}

export function hasNativeContinuation(body: Record<string, unknown>, headers?: Headers): boolean {
  if (body.previous_response_id || body._omniroutePreviousResponseResumed ||
      body.session_id || body.conversation_id || body.prompt_cache_key ||
      hasNativeCodexTurnBinding(body) || hasCodexIdentityHeaders(headers) ||
      Boolean(headers?.get("x-codex-turn-state") || headers?.get("x-claude-code-session-id"))) return true;
  for (const list of [body.messages, body.input]) {
    if (!Array.isArray(list)) continue;
    if (list.some((item) => item && typeof item === "object" &&
      ((item as Record<string, unknown>).role === "assistant" ||
       (item as Record<string, unknown>).role === "tool" ||
       ["function_call", "function_call_output", "reasoning"].includes(String((item as Record<string, unknown>).type))))) return true;
  }
  return false;
}

export interface NativeRoutingPolicy {
  enabled: boolean;
  subscriptionOnlyKeyIds: string[];
}

export function parseNativeRoutingPolicy(value: unknown): NativeRoutingPolicy {
  const row = value && typeof value === "object" ? value as Record<string, unknown> : {};
  return {
    enabled: row.enabled === true,
    subscriptionOnlyKeyIds: Array.isArray(row.subscriptionOnlyKeyIds)
      ? row.subscriptionOnlyKeyIds.filter((id): id is string => typeof id === "string") : [],
  };
}

export async function blocksNativeSubscriptionOnlyKey(keyId: string | null, provider: string): Promise<boolean> {
  if (!keyId || provider === "codex" || provider === "claude") return false;
  const settings = await getCachedSettings();
  return parseNativeRoutingPolicy(settings.nativeSubscriptionRouting).subscriptionOnlyKeyIds.includes(keyId);
}

export async function subscriptionOnlyConnections(keyId: string | null, provider: string, allowed: string[] | null): Promise<string[] | null> {
  if (!keyId) return allowed;
  const settings = await getCachedSettings();
  const policy = parseNativeRoutingPolicy(settings.nativeSubscriptionRouting);
  if (!policy.subscriptionOnlyKeyIds.includes(keyId) &&
      !(policy.enabled && (provider === "codex" || provider === "claude"))) return allowed;
  const connections = await getProviderConnections({ provider, isActive: true }, undefined, undefined,
    ["id", "provider", "auth_type", "is_active", "provider_specific_data"]);
  return connections.filter(isRoutingConnection).filter((connection) => {
    const verdict = classifyConnectionBilling({ provider: connection.provider, authType: connection.authType, connectionId: connection.id });
    return verdict.billing === "subscription" && isOverageSafe(verdict) &&
      !isClaudeExtraUsageAllowed(connection.provider, connection.providerSpecificData) &&
      !isCodexPaidCreditsEnabled(connection.provider, connection.providerSpecificData) &&
      (allowed === null || allowed.includes(connection.id));
  }).map((connection) => connection.id);
}

/** Resolve only explicitly observed exact-model API connections in the paired family. */
export async function runNativePaidFallback(args: {
  response: Response;
  provider: string;
  model: string;
  body: Record<string, unknown>;
  apiKey: string | null;
  keyId: string | null;
  allowedConnections: string[] | null;
  allowedQuotas?: string[];
  headers?: Headers;
  dispatch: (provider: string, model: string, connectionId: string) => Promise<Response>;
}): Promise<Response> {
  if (!safeRefusals.has(args.response) || !args.apiKey || !args.keyId || hasNativeContinuation(args.body, args.headers)) return args.response;
  const family = args.provider === "codex" ? "openai" : args.provider === "claude" ? "anthropic" : null;
  if (!family) return args.response;
  const settings = await getCachedSettings();
  const policy = parseNativeRoutingPolicy(settings.nativeSubscriptionRouting);
  if (!policy.enabled || policy.subscriptionOnlyKeyIds.includes(args.keyId)) return args.response;
  markNativeResponseTerminal(args.response);
  if (!(await isModelAllowedForKey(args.apiKey, `${family}/${args.model}`, args.model))) return args.response;
  const connections = await getProviderConnections({ provider: family, isActive: true });
  const observed = await Promise.all(connections.filter(isRoutingConnection).map(async (connection) => ({
    connection,
    models: await getSyncedAvailableModelsForConnection(family, connection.id),
  })));
  let allowed = args.allowedConnections ?? undefined;
  if (args.allowedQuotas?.length) {
    const scope = await resolveQuotaKeyScope(args.allowedQuotas);
    if (scope.connectionIds.length === 0) return args.response;
    allowed = allowed === undefined ? scope.connectionIds : allowed.filter((id) => scope.connectionIds.includes(id));
  }
  const candidates = observed.flatMap(({ connection, models }) =>
    (connection.authType === "apikey" || connection.authType === "api_key") &&
    models.some((entry) => entry.id === args.model && entry.visibility !== "hide")
      ? [{ providerFamily: family, upstreamModel: args.model, connectionId: connection.id, billing: "api" as const, priority: Number(connection.priority) || 0 }]
      : []
  );
  candidates.sort((a, b) => a.priority - b.priority);
  const eligible = selectNativePaidFallbacks({ providerFamily: family, upstreamModel: args.model }, candidates, {
    subscriptionOnly: policy.subscriptionOnlyKeyIds.includes(args.keyId),
    allowedConnectionIds: allowed,
  });
  // One paid dispatch only. Failure is returned, never replayed on another paid key.
  if (!eligible.length) return args.response;
  const response = await args.dispatch(family, args.model, eligible[0].connectionId);
  if (args.response.body) await args.response.body.cancel().catch(() => {});
  return markNativeResponseTerminal(response);
}

/** A native failed dispatch is replayable only after an explicit upstream rejection. */
export function isConfirmedNativeRejection(result: {
  status?: number;
  errorCode?: string;
  errorType?: string;
  response?: Response;
}, upstreamStatus?: number): boolean {
  return ![result.errorCode, result.errorType].some((value) =>
    ["chatgpt_submission_ambiguous", "STREAM_EARLY_EOF", "STREAM_READINESS_TIMEOUT", "stream_incomplete", "stream_timeout", "stream_early_eof", "upstream_timeout"].includes(value ?? "")) &&
    typeof upstreamStatus === "number" && [401, 403, 429].includes(upstreamStatus) &&
    result.status === upstreamStatus && result.response?.ok === false;
}
