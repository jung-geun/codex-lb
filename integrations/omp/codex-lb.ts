import { constants } from "node:fs";
import { link, lstat, mkdir, open, readFile, unlink } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { getAgentDir } from "@oh-my-pi/pi-coding-agent";
import type { ExtensionAPI, ExtensionContext, ModelRegistry, ProviderConfigInput } from "@oh-my-pi/pi-coding-agent";
import { resolveConfigValue } from "@oh-my-pi/pi-coding-agent/config/resolve-config-value";
import { installUpdateCheck } from "./update-check";

// Standalone OMP plugin; connect-omp.py's adjacent settings remain supported.
// No custom inference transport, OAuth refresh, shell commands, or remote code loading.
const gatewayProfiles = {
  "codex-lb": { environment: "CODEX_LB", commandPrefix: "lb", statusLabel: "LB" },
  omniroute: { environment: "OMNIROUTE", commandPrefix: "omniroute", statusLabel: "OmniRoute" },
} as const;
type GatewayProvider = keyof typeof gatewayProfiles;

const VERSION = 1;
const MAX_BYTES = 2 * 1024 * 1024;
const roots: Record<string, readonly string[]> = {
  "openai-codex-responses": ["/backend-api/codex"],
  "anthropic-messages": ["/"],
  "openai-completions": ["/v1"],
};
const required = ["id", "name", "api", "basePath", "reasoning", "input", "contextWindow", "maxTokens"];
const optional = ["thinking", "compat", "preferWebsockets", "remoteCompaction"];
const efforts = ["minimal", "low", "medium", "high", "xhigh", "max"];
const thinkingFlags = ["supportsDisplay", "prefixBinding", "suppressWhenOff", "requiresEffort", "unverified"];
const compatFlags = ["supportsStore", "supportsDeveloperRole", "supportsReasoningEffort", "supportsUsageInStreaming", "supportsStrictMode", "supportsToolChoice", "supportsParallelToolCalls"];
const compactionPaths = ["/responses/compact", "/responses/compact/stream", "/responses/compact/v2"];
type Row = Record<string, unknown>;
type NativeModel = NonNullable<ProviderConfigInput["models"]>[number];

function reject(): never {
  // Never include response text, URLs with credentials, keys, or server errors.
  throw new Error("Invalid codex-lb v1 model contract");
}
function record(value: unknown, allowed: readonly string[], mandatory: readonly string[] = []): Row {
  if (!value || typeof value !== "object" || Array.isArray(value)) return reject();
  const row = value as Row;
  if (Object.keys(row).some(key => !allowed.includes(key)) || mandatory.some(key => !Object.hasOwn(row, key))) return reject();
  return row;
}
function text(value: unknown): value is string {
  return typeof value === "string";
}
function integer(value: unknown): boolean {
  return Number.isSafeInteger(value) && (value as number) > 0 && (value as number) <= 100_000_000;
}
export function validateOrigin(value: unknown): string {
  if (!text(value) || /[\s\\]/.test(value)) return reject();
  const url = new URL(value);
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password || url.search || url.hash || url.pathname !== "/" || value.replace(/\/$/, "") !== url.origin) return reject();
  return url.origin;
}

export function validateManifest(value: unknown): Row[] {
  const document = record(value, ["schemaVersion", "models"], ["schemaVersion", "models"]);
  if (document.schemaVersion !== VERSION || !Array.isArray(document.models) || document.models.length > 4096) return reject();
  const seen = new Set<string>();
  return document.models.map(value => {
    const row = record(value, [...required, ...optional], required);
    if (!text(row.id) || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(row.id) || seen.has(row.id)) return reject();
    seen.add(row.id);
    if (!text(row.name) || !row.name || row.name.length > 256 || /[\x00-\x1f]/.test(row.name)) return reject();
    if (!text(row.api) || !Object.hasOwn(roots, row.api) || !text(row.basePath) || !roots[row.api].includes(row.basePath)) return reject();
    if (typeof row.reasoning !== "boolean" || !integer(row.contextWindow) || !integer(row.maxTokens)) return reject();
    if (!Array.isArray(row.input) || !row.input.length || row.input.some(x => x !== "text" && x !== "image") || new Set(row.input).size !== row.input.length) return reject();
    if (Object.hasOwn(row, "preferWebsockets") && (typeof row.preferWebsockets !== "boolean" || row.api !== "openai-codex-responses")) return reject();
    if (Object.hasOwn(row, "thinking")) {
      const thinking = record(row.thinking, ["mode", "efforts", "defaultLevel", "effortMap", "effortBudgets", ...thinkingFlags], ["mode", "efforts"]);
      if (!row.reasoning || !text(thinking.mode) || !["effort", "budget", "google-level", "anthropic-adaptive", "anthropic-budget-effort"].includes(thinking.mode)) return reject();
      if (!Array.isArray(thinking.efforts) || !thinking.efforts.length || thinking.efforts.some(x => !efforts.includes(x)) || new Set(thinking.efforts).size !== thinking.efforts.length) return reject();
      if (Object.hasOwn(thinking, "defaultLevel") && !thinking.efforts.includes(thinking.defaultLevel)) return reject();
      for (const key of thinkingFlags) if (Object.hasOwn(thinking, key) && typeof thinking[key] !== "boolean") return reject();
      for (const key of ["effortMap", "effortBudgets"]) {
        if (!Object.hasOwn(thinking, key)) continue;
        const map = record(thinking[key], efforts);
        for (const entry of Object.values(map)) {
          if (key === "effortBudgets" ? !integer(entry) : !text(entry) || !/^[A-Za-z0-9_-]{1,40}$/.test(entry)) return reject();
        }
      }
    }
    if (Object.hasOwn(row, "compat")) {
      const compat = record(row.compat, [...compatFlags, "maxTokensField"]);
      for (const key of compatFlags) if (Object.hasOwn(compat, key) && typeof compat[key] !== "boolean") return reject();
      if (Object.hasOwn(compat, "maxTokensField") && compat.maxTokensField !== "max_tokens" && compat.maxTokensField !== "max_completion_tokens") return reject();
    }
    if (Object.hasOwn(row, "remoteCompaction")) {
      const config = record(row.remoteCompaction, ["enabled", "api", "endpoint", "v2StreamingEnabled", "v2Endpoint", "streamingEndpoint"]);
      if (row.api !== "openai-codex-responses" || !row.id.startsWith("gpt-")) return reject();
      for (const [key, entry] of Object.entries(config)) {
        if (key === "enabled" || key === "v2StreamingEnabled") {
          if (typeof entry !== "boolean") return reject();
        } else if (key === "api") {
          if (entry !== "openai-codex-responses") return reject();
        } else if (!text(entry) || !compactionPaths.includes(entry)) return reject();
      }
    }
    return structuredClone(row);
  });
}

export function materializeManifest(value: unknown, baseOrigin: string): NativeModel[] {
  const origin = validateOrigin(baseOrigin);
  return validateManifest(value).map(row => {
    const { basePath, ...model } = row;
    // The gateway selects native subscriptions or HTTP-only paid APIs per turn.
    if (model.api === "openai-codex-responses") model.preferWebsockets = false;
    if (model.remoteCompaction) {
      const config = model.remoteCompaction as Row;
      for (const key of ["endpoint", "v2Endpoint", "streamingEndpoint"]) {
        if (Object.hasOwn(config, key)) config[key] = origin + "/backend-api/codex" + config[key];
      }
    }
    // registerProvider's official dynamic factory feeds buildCustomModelOverlay,
    // which marks anthropic-messages as isOAuth when no provider auth override
    // is supplied. Do not register an API-key auth mode or custom streamer.
    // Cost is intentionally absent: native materialization uses catalog estimates;
    // the manifest does not claim subscription calls have a free metered price.
    return { ...model, baseUrl: origin + basePath } as NativeModel;
  });
}

async function fetchModelDocument(baseOrigin: string, key: string, path: string, request: typeof fetch): Promise<unknown> {
  const origin = validateOrigin(baseOrigin);
  if (!key || !/^[\x21-\x7e]+$/.test(key)) throw new Error("codex-lb requires a nonempty gateway key");
  const response = await request(origin + path, {
    headers: { Authorization: `Bearer ${key}` }, redirect: "error", signal: AbortSignal.timeout(12_000),
  });
  if (!response.ok || !response.body) throw new Error("codex-lb authenticated discovery unavailable; static fallback retained");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      length += value.length;
      if (length > MAX_BYTES) return reject();
      chunks.push(value);
    }
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}

export async function discoverModels(baseOrigin: string, key: string, request: typeof fetch = fetch): Promise<NativeModel[]> {
  return materializeManifest(await fetchModelDocument(baseOrigin, key, "/v1/omp/models", request), baseOrigin);
}

// OMP18.12.3 official Codex catalog supplies output/thinking metadata absent
// from the native upstream catalog. Never register an unobserved/unknown ID.
async function discoverCodexModels(origin: string, key: string, request: typeof fetch): Promise<NativeModel[]> {
  const document = await fetchModelDocument(origin, key, "/backend-api/codex/models", request);
  if (!document || typeof document !== "object" || !("models" in document) || !Array.isArray(document.models)) return reject();
  const rows: unknown[] = document.models;
  if (rows.length > 4096) return reject();
  const defaults = validateManifest(JSON.parse(await readFile(new URL("./codex-defaults.json", import.meta.url), "utf8")));
  const observed = new Map<string, Row>();
  for (const value of rows) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return reject();
    const row = value as Row;
    if (!text(row.slug) || observed.has(row.slug)) return reject();
    observed.set(row.slug, row);
  }
  const models = defaults.flatMap(model => {
    const row = observed.get(model.id as string);
    if (!row || row.supported_in_api === false || row.visibility === "hide" || !integer(row.context_window)) return [];
    return [{ ...model, contextWindow: row.context_window }];
  });
  return materializeManifest({ schemaVersion: VERSION, models }, origin);
}

async function discoverGatewayModels(origin: string, key: string): Promise<NativeModel[]> {
  const [manifest, native] = await Promise.allSettled([
    discoverModels(origin, key), discoverCodexModels(origin, key, fetch),
  ]);
  if (manifest.status === "rejected" && native.status === "rejected") throw new Error("codex-lb discovery unavailable");
  // An explicit gateway manifest wins over bundled metadata for the same ID.
  const models = new Map<string, NativeModel>();
  if (native.status === "fulfilled") for (const model of native.value) models.set(model.id, model);
  if (manifest.status === "fulfilled") for (const model of manifest.value) models.set(model.id, model);
  return [...models.values()];
}

export interface QuotaWindow {
  window: string;
  remaining_account_equivalents: number | null;
  reporting_pools: number;
  unknown_pools: number;
  remaining_percent: number | null;
  observed_at: string | null;
  resets_at: string | null;
}
export interface ClientQuota {
  model: string;
  provider: "codex" | "claude" | null;
  generated_at: string;
  hidden: boolean;
  windows: QuotaWindow[];
}
const QUOTA_BYTES = 64 * 1024;
const QUOTA_REFRESH_MS = 60_000;

function quotaError(): never {
  throw new Error("codex-lb quota unavailable");
}
function timestamp(value: unknown): value is string {
  return text(value) && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value));
}
function count(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) <= 1_000_000;
}
function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}
export function parseQuota(value: unknown, model: string, now = Date.now()): ClientQuota {
  const row = record(value, ["model", "provider", "generated_at", "hidden", "windows"], ["model", "provider", "generated_at", "hidden", "windows"]);
  if (row.model !== model || !["codex", "claude", null].includes(row.provider as string | null) || typeof row.hidden !== "boolean" || !timestamp(row.generated_at)) return quotaError();
  const age = now - Date.parse(row.generated_at);
  if (age < -30_000 || age > 300_000 || !Array.isArray(row.windows) || row.windows.length > 32) return quotaError();
  if ((row.hidden || row.provider === null) && row.windows.length) return quotaError();
  const seen = new Set<string>();
  const windows = row.windows.map(value => {
    const fields = ["window", "remaining_account_equivalents", "reporting_pools", "unknown_pools", "remaining_percent", "observed_at", "resets_at"];
    const window = record(value, fields, fields);
    if (!text(window.window) || !/^[A-Za-z0-9_-]{1,64}$/.test(window.window) || seen.has(window.window)) return quotaError();
    seen.add(window.window);
    if (!count(window.reporting_pools) || !count(window.unknown_pools)) return quotaError();
    const { reporting_pools: reporting, unknown_pools: unknown, remaining_account_equivalents: remaining, remaining_percent: percent } = window;
    if (reporting === 0) {
      if (remaining !== null || percent !== null || window.observed_at !== null || window.resets_at !== null) return quotaError();
    } else {
      if (!finite(remaining) || remaining < 0 || remaining > reporting || !timestamp(window.observed_at)) return quotaError();
      const observationAge = now - Date.parse(window.observed_at);
      if (observationAge < -30_000 || observationAge > 300_000) return quotaError();
      if (unknown) {
        if (percent !== null) return quotaError();
      } else if (!finite(percent) || percent < 0 || percent > 100 || Math.abs(percent - remaining / reporting * 100) > 0.000001) return quotaError();
      if (window.resets_at !== null && (!timestamp(window.resets_at) || Date.parse(window.resets_at) <= now)) return quotaError();
    }
    return window as unknown as QuotaWindow;
  });
  return { model, provider: row.provider as ClientQuota["provider"], generated_at: row.generated_at, hidden: row.hidden, windows };
}

export async function fetchQuota(baseOrigin: string, key: string, model: string, signal?: AbortSignal, request: typeof fetch = fetch): Promise<ClientQuota> {
  try {
    const origin = validateOrigin(baseOrigin);
    if (!key || !/^[\x21-\x7e]+$/.test(key) || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(model)) return quotaError();
    const boundedSignal = AbortSignal.any([AbortSignal.timeout(8_000), ...(signal ? [signal] : [])]);
    boundedSignal.throwIfAborted();
    const response = await request(origin + "/v1/usage/quota?model=" + encodeURIComponent(model), {
      headers: { Authorization: `Bearer ${key}`, Accept: "application/json" }, redirect: "error", cache: "no-store", signal: boundedSignal,
    });
    if (!response.ok || !response.body || response.redirected || (response.url && new URL(response.url).origin !== origin)) return quotaError();
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let length = 0;
    try {
      for (;;) {
        boundedSignal.throwIfAborted();
        const { value, done } = await reader.read();
        if (done) break;
        length += value.length;
        if (length > QUOTA_BYTES) return quotaError();
        chunks.push(value);
      }
    } finally {
      await reader.cancel();
      reader.releaseLock();
    }
    boundedSignal.throwIfAborted();
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    return parseQuota(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)), model);
  } catch {
    // No provider response, raw transport exception, URL, or credential reaches UI.
    return quotaError();
  }
}

export function formatQuota(quota: ClientQuota, label = "LB"): string {
  const provider = quota.provider === "codex" ? "Codex" : quota.provider === "claude" ? "Claude" : "selected model";
  if (quota.hidden) return `${label} ${provider} quota hidden`;
  if (quota.provider === null) return `${label} selected model: no subscription quota`;
  const windows = ["5h", "7d"].map(label => {
    const row = quota.windows.find(window => window.window === label);
    if (!row) return `${label} unknown (no pool observations)`;
    const percent = row.remaining_percent === null ? "unknown" : `${Number(row.remaining_percent.toFixed(1))}%`;
    const equivalent = row.remaining_account_equivalents === null ? "unknown" : Number(row.remaining_account_equivalents.toFixed(2));
    return `${label} ${percent} (${equivalent} pool eq; ${row.reporting_pools}/${row.reporting_pools + row.unknown_pools} reporting, ${row.unknown_pools} unknown)`;
  });
  return `${label} ${provider} remaining · ${windows.join(" · ")}`;
}

// Only official context/UI/timer APIs; no inference/auth interception or OMP patch.
export function installQuotaDisplay(pi: ExtensionAPI, origin: string, gatewayKey: () => string, request: typeof fetch = fetch, provider: GatewayProvider = "codex-lb"): void {
  const { commandPrefix, statusLabel } = gatewayProfiles[provider];
  const quotaUiKey = `${provider}-quota`;
  let context: ExtensionContext | undefined;
  let timer: Timer | undefined;
  let controller: AbortController | undefined;
  let generation = 0;
  let selected: string | undefined;
  let refreshed = 0;
  let stopped = false;
  let message: string | undefined;
  const current = () => {
    const model = context?.models.current();
    return model?.provider === provider ? model.id : undefined;
  };
  const show = (status?: string, detail?: string) => {
    message = detail ?? status;
    context?.ui.setStatus(quotaUiKey, status);
    // Compact status belongs beside the host's context segment; details are manual.
    context?.ui.setWidget(quotaUiKey, undefined);
  };
  const cancel = () => { generation++; controller?.abort(); controller = undefined; };
  const refresh = async (force = false): Promise<void> => {
    if (stopped) return;
    const model = current();
    if (model !== selected) {
      cancel();
      selected = model;
      refreshed = 0;
      show();
    }
    if (!model || (!force && (controller || Date.now() - refreshed < QUOTA_REFRESH_MS))) return;
    cancel();
    const ownGeneration = generation;
    const ownController = new AbortController();
    controller = ownController;
    refreshed = Date.now();
    show(`${statusLabel} quota refreshing`);
    try {
      const quota = await fetchQuota(origin, gatewayKey(), model, ownController.signal, request);
      if (!stopped && generation === ownGeneration && !ownController.signal.aborted && current() === model) {
        if (quota.hidden || quota.provider === null) show(formatQuota(quota, statusLabel));
        else {
          const windows = ["5h", "7d"].map(label => {
            const row = quota.windows.find(window => window.window === label);
            return `${label} ${row?.remaining_percent == null ? "unknown" : `${Number(row.remaining_percent.toFixed(1))}%`}`;
          });
          show(`${statusLabel} ${quota.provider === "codex" ? "Codex" : "Claude"} · ${windows.join(" · ")}`, formatQuota(quota, statusLabel));
        }
      }
    } catch {
      if (!stopped && generation === ownGeneration && !ownController.signal.aborted && current() === model) show(`${statusLabel} quota unavailable`);
    } finally {
      if (generation === ownGeneration) {
        controller = undefined;
        // A model can switch while awaiting IO, before the polling tick observes it.
        if (current() !== model) { selected = undefined; show(); void refresh(); }
      }
    }
  };
  pi.on("session_start", (_event, next) => {
    if (timer && context) context.clearTimer(timer);
    cancel();
    context = next;
    stopped = false;
    selected = undefined;
    refreshed = 0;
    // There is no official model-select event. Live current() polling catches
    // idle picker changes; turn hooks refresh immediately before inference.
    timer = next.setInterval(() => { void refresh(); }, 500);
    void refresh();
  });
  for (const event of ["before_agent_start", "turn_start", "turn_end"] as const) {
    pi.on(event, (_event, next) => { context = next; void refresh(!controller); });
  }
  pi.on("session_shutdown", (_event, next) => {
    stopped = true;
    cancel();
    if (timer) next.clearTimer(timer);
    timer = undefined;
    show();
    context = undefined;
  });
  pi.registerCommand(`${commandPrefix}-quota`, {
    description: `Refresh selected ${provider} model's subscription quota (pool equivalents, not token credit)`,
    handler: async (_args, next) => {
      context = next;
      const requested = current();
      await refresh(true);
      if (!current()) next.ui.notify(`Select a ${provider} model to view its subscription quota`, "info");
      else if (!stopped && requested === current() && message) next.ui.notify(message, "info");
    },
  });
}

const guardedGatewayKeys = new WeakMap<ModelRegistry, Set<string>>();

export function installGatewayKeyGuard(registry: ModelRegistry, keyEnv: string): void {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(keyEnv)) return reject();
  const existing = guardedGatewayKeys.get(registry);
  if (existing) { existing.add(keyEnv); return; }
  const guarded = new Set([keyEnv]);
  guardedGatewayKeys.set(registry, guarded);
  // The loader records extension initialization errors and continues with YAML.
  // Guard the official auth resolver itself, including static/offline models,
  // rather than throwing before registration and leaving the literal env name.
  registry.authStorage.keys.setResolver(async reference => {
    if (!guarded.has(reference)) return await resolveConfigValue(reference);
    const key = process.env[reference];
    if (!key || !/^[\x21-\x7e]+$/.test(key)) throw new Error("Gateway key environment variable is missing or empty");
    return key;
  });
}

interface GatewaySettings { schemaVersion: 1; baseOrigin: string; keyEnv: string }

function parseGatewaySettings(raw: string): GatewaySettings {
  const settings = record(JSON.parse(raw), ["schemaVersion", "baseOrigin", "keyEnv"], ["schemaVersion", "baseOrigin", "keyEnv"]);
  if (settings.schemaVersion !== VERSION || !text(settings.keyEnv) || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(settings.keyEnv)) return reject();
  return { schemaVersion: VERSION, baseOrigin: validateOrigin(settings.baseOrigin), keyEnv: settings.keyEnv };
}

async function readOptionalSettings(path: string | URL): Promise<GatewaySettings | undefined> {
  try { return parseGatewaySettings(await readFile(path, "utf8")); }
  catch (error) {
    if (!error || typeof error !== "object" || !("code" in error) || error.code !== "ENOENT") throw error;
  }
}

async function saveOmnirouteConnection(argument: string): Promise<string> {
  // Validate the entire argument and environment before even reading settings.
  // An API key, extra argument, or URL with credentials is never accepted.
  const origin = validateOrigin(argument);
  const environmentOrigin = process.env.OMNIROUTE_BASE_URL;
  if (environmentOrigin !== undefined && validateOrigin(environmentOrigin) !== origin) throw new Error("Connection conflict");
  const environmentKey = process.env.OMNIROUTE_KEY_ENV;
  if (environmentKey !== undefined && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(environmentKey)) throw new Error("Connection conflict");
  const profileDirectory = getAgentDir();
  const directory = join(profileDirectory, "extensions");
  const filename = join(directory, "omniroute-v1.json");
  for (const parent of [profileDirectory, directory]) {
    try {
      if (!(await lstat(parent)).isDirectory()) throw new Error("Connection conflict");
    } catch (error) {
      if (!error || typeof error !== "object" || !("code" in error) || error.code !== "ENOENT") throw error;
    }
  }
  try {
    if (!(await lstat(filename)).isFile()) throw new Error("Connection conflict");
  } catch (error) {
    if (!error || typeof error !== "object" || !("code" in error) || error.code !== "ENOENT") throw error;
  }
  const adjacent = await readOptionalSettings(new URL("./omniroute-v1.json", import.meta.url));
  const existing = await readOptionalSettings(filename);
  for (const settings of [adjacent, existing]) {
    if (settings && settings.baseOrigin !== origin) throw new Error("Connection conflict");
  }
  if (adjacent && existing && adjacent.keyEnv !== existing.keyEnv) throw new Error("Connection conflict");
  const keyEnv = existing?.keyEnv ?? adjacent?.keyEnv ?? environmentKey ?? "OMNIROUTE_API_KEY";
  // Environment configuration wins at startup, so do not save a misleading
  // profile if that override would select a different credential variable.
  if (environmentOrigin !== undefined && environmentKey !== undefined && environmentKey !== keyEnv) throw new Error("Connection conflict");
  if (environmentOrigin !== undefined && environmentKey === undefined && keyEnv !== "OMNIROUTE_API_KEY") throw new Error("Connection conflict");
  if (existing) {
    const file = await open(filename, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const current = parseGatewaySettings(await file.readFile("utf8"));
      if (current.baseOrigin !== origin || current.keyEnv !== keyEnv) throw new Error("Connection conflict");
      await file.chmod(0o600);
    } finally { await file.close(); }
    return keyEnv;
  }
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const temporary = join(directory, `.omniroute-v1-${randomUUID()}.tmp`);
  const file = await open(temporary, "wx", 0o600);
  try {
    try {
      await file.writeFile(JSON.stringify({ schemaVersion: VERSION, baseOrigin: origin, keyEnv }) + "\n", "utf8");
      await file.chmod(0o600);
      await file.sync();
    } finally { await file.close(); }
    // Hard-link publication is atomic and refuses a concurrently created file;
    // rename would silently overwrite someone else's new configuration.
    await link(temporary, filename);
  } finally {
    await unlink(temporary);
  }
  return keyEnv;
}

async function registerGateway(pi: ExtensionAPI, provider: GatewayProvider): Promise<void> {
  const { environment, commandPrefix } = gatewayProfiles[provider];
  let origin: string | undefined;
  let keyEnv = process.env[`${environment}_KEY_ENV`] ?? `${environment}_API_KEY`;
  let invalidConfiguration = false;
  try {
    const configuredEnvironmentOrigin = process.env[`${environment}_BASE_URL`];
    if (configuredEnvironmentOrigin !== undefined) {
      origin = validateOrigin(configuredEnvironmentOrigin);
    } else {
      const filename = `${provider}-v1.json`;
      const settings = await readOptionalSettings(new URL(`./${filename}`, import.meta.url))
        ?? await readOptionalSettings(join(getAgentDir(), "extensions", filename));
      if (settings) { origin = settings.baseOrigin; keyEnv = settings.keyEnv; }
    }
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(keyEnv)) return reject();
  } catch (error) {
    if (provider !== "omniroute") throw error;
    // Keep setup help available, but never register an invalid connection.
    invalidConfiguration = true;
    origin = undefined;
    keyEnv = "OMNIROUTE_API_KEY";
  }
  const configuredOrigin = origin;
  pi.registerCommand(`${commandPrefix}-connect`, {
    description: provider === "omniroute"
      ? "Show connection help, or save an HTTPS origin: /omniroute-connect https://your-gateway.example"
      : "Show codex-lb connection information and setup instructions (never displays the key)",
    handler: async (args, context) => {
      if (provider === "omniroute" && args.trim()) {
        try {
          const savedKeyEnv = await saveOmnirouteConnection(args.trim());
          context.ui.notify(`OmniRoute connection saved for this OMP profile. Supply ${savedKeyEnv} through your environment or secret manager and restart OMP. No key was stored, no discovery or inference was sent, and model roles were not changed. OMNIROUTE_BASE_URL overrides the saved origin; with that override, OMNIROUTE_KEY_ENV (default OMNIROUTE_API_KEY) selects the key variable.`, "info");
        } catch {
          context.ui.notify("OmniRoute setup refused. Use exactly one suffix-free HTTPS origin, without credentials, a key, or extra arguments. Existing adjacent/profile settings must be valid and match the origin and key-variable selection; conflicting environment overrides must be unset or corrected. No existing configuration was overwritten.", "error");
        }
        return;
      }
      const keyPresent = Boolean(process.env[keyEnv] && /^[\x21-\x7e]+$/.test(process.env[keyEnv]!));
      context.ui.notify([
        `${provider} OMP provider`,
        invalidConfiguration ? "Gateway: invalid configuration (not registered)" : configuredOrigin ? `Gateway: ${configuredOrigin}` : "Gateway: not configured",
        `Key environment: ${keyEnv} (${keyPresent ? "present" : "missing or invalid"})`,
        "Setup: obtain your gateway URL and gateway API key from its administrator / APIs page.",
        ...(provider === "omniroute" ? ["Save for this profile: /omniroute-connect https://your-gateway.example (origin only; never pass a key). Restart OMP afterward."] : []),
        `Set ${environment}_BASE_URL=https://your-gateway.example and ${environment}_API_KEY in your shell or secret manager, then restart OMP.`,
        `For a different key variable, set ${environment}_KEY_ENV to its name. Never use an upstream subscription token or provider API key here.`,
        `Models: /${commandPrefix}-models; refresh from a terminal with omp models refresh, then restart OMP and select /model ${provider}/<id>.`,
        `Quota: /${commandPrefix}-quota (selected model's provider only; not a token balance). No inference is sent by these commands.`,
      ].join("\n"), "info");
    },
  });
  pi.registerCommand(`${commandPrefix}-models`, {
    description: `List available ${provider} models, protocols, and context/output limits`,
    handler: async (_args, context) => {
      const models = context.models.list().filter(model => model.provider === provider);
      context.ui.notify(models.length ? models.map(model =>
        `${provider}/${model.id} · ${model.api} · context ${model.contextWindow} · output ${model.maxTokens}`,
      ).join("\n") : `No ${provider} models available. Use /${commandPrefix}-connect to configure the gateway and key, then run omp models refresh and restart OMP. Model visibility depends on your gateway key's access.`, "info");
    },
  });
  if (!origin) return;
  const gatewayKey = () => {
    const value = process.env[keyEnv];
    if (!value || !/^[\x21-\x7e]+$/.test(value)) throw new Error(`${provider} gateway key environment variable is missing or empty`);
    return value;
  };
  pi.on("session_start", (_event, context) => {
    installGatewayKeyGuard(context.modelRegistry, keyEnv);
  });
  installQuotaDisplay(pi, origin, gatewayKey, fetch, provider);
  pi.registerProvider(provider, {
    apiKey: keyEnv,
    // No provider baseUrl/API/auth/compat/compaction override: native per-model
    // roots and preserved YAML static fallbacks must survive registration.
    fetchDynamicModels: async () => {
      try {
        // OmniRoute's key-scoped manifest is the complete visibility boundary,
        // including an empty success. Only legacy codex-lb supplements it with
        // the native Codex catalog for older gateway deployments.
        return provider === "omniroute"
          ? await discoverModels(configuredOrigin!, gatewayKey())
          : await discoverGatewayModels(configuredOrigin!, gatewayKey());
      }
      catch { throw new Error(`${provider} discovery failed; native static fallback retained`); }
    },
  });
}

export default async function codexLb(pi: ExtensionAPI): Promise<void> {
  installUpdateCheck(pi);
  await registerGateway(pi, "codex-lb");
  await registerGateway(pi, "omniroute");
}
