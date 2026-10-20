import { del, get, patch, post } from "@/lib/api-client";

import {
  ModelSourceCreateRequestSchema,
  ModelSourceApiUsageSchema,
  ModelSourceQuotaHistorySchema,
  ModelSourceSchema,
  ModelSourcesResponseSchema,
  ModelSourceUsageSummarySchema,
  ModelSourceUpdateRequestSchema,
  SourceOAuthStartSchema,
  SourceOAuthStatusSchema,
  SourcePresetsSchema,
} from "@/features/model-sources/schemas";

const MODEL_SOURCES_PATH = "/api/model-sources";

export function listModelSources(signal?: AbortSignal) {
  return get(`${MODEL_SOURCES_PATH}/`, ModelSourcesResponseSchema, { signal });
}

export function createModelSource(payload: unknown) {
  const validated = ModelSourceCreateRequestSchema.parse(payload);
  return post(`${MODEL_SOURCES_PATH}/`, ModelSourceSchema, {
    body: validated,
  });
}

export function updateModelSource(sourceId: string, payload: unknown) {
  const validated = ModelSourceUpdateRequestSchema.parse(payload);
  return patch(`${MODEL_SOURCES_PATH}/${encodeURIComponent(sourceId)}`, ModelSourceSchema, {
    body: validated,
  });
}

export function deleteModelSource(sourceId: string) {
  return del(`${MODEL_SOURCES_PATH}/${encodeURIComponent(sourceId)}`);
}

function sourcePath(sourceId: string) {
  return `${MODEL_SOURCES_PATH}/${encodeURIComponent(sourceId)}`;
}

export function startSourceOAuth(sourceId: string) {
  return post(`${sourcePath(sourceId)}/oauth/start`, SourceOAuthStartSchema);
}

export function getSourceOAuthStatus(sourceId: string, flowId: string, signal?: AbortSignal) {
  return get(`${sourcePath(sourceId)}/oauth/status?flowId=${encodeURIComponent(flowId)}`, SourceOAuthStatusSchema, { signal });
}

export function completeSourceOAuth(sourceId: string, flowId: string, code: string) {
  return post(`${sourcePath(sourceId)}/oauth/complete`, ModelSourceSchema, {
    body: { flowId, code },
  });
}

export function cancelSourceOAuth(sourceId: string, flowId: string) {
  return del(`${sourcePath(sourceId)}/oauth/flows/${encodeURIComponent(flowId)}`);
}

export function importSourceCredentials(sourceId: string, credentials: Record<string, unknown>) {
  return post(`${sourcePath(sourceId)}/credentials/import`, ModelSourceSchema, {
    body: { credentials, singleWriterConfirmed: true },
  });
}

export function refreshSourceQuota(sourceId: string) {
  return post(`${sourcePath(sourceId)}/quota`, ModelSourceSchema);
}

export function warmupSource(sourceId: string) {
  return post(`${sourcePath(sourceId)}/warmup`, ModelSourceSchema);
}

export function listSourcePresets() {
  return get(`${MODEL_SOURCES_PATH}/presets`, SourcePresetsSchema);
}

export function getModelSourceUsage(sourceId: string, signal?: AbortSignal, days: 1 | 7 | 30 = 1) {
  return get(`${sourcePath(sourceId)}/usage?days=${days}`, ModelSourceUsageSummarySchema, { signal });
}

export function getModelSourceApiUsage(sourceId: string, signal?: AbortSignal) {
  return get(`${sourcePath(sourceId)}/api-usage`, ModelSourceApiUsageSchema, { signal });
}

export function getModelSourceQuotaHistory(sourceId: string, signal?: AbortSignal) {
  return get(`${sourcePath(sourceId)}/quota-history?days=7`, ModelSourceQuotaHistorySchema, { signal });
}
