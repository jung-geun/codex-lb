import { describe, expect, spyOn, test } from "bun:test";
import { pathToFileURL } from "node:url";
import { join } from "node:path";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { discoverModels, fetchQuota, formatQuota, installGatewayKeyGuard, installQuotaDisplay, materializeManifest, parseQuota, validateManifest, validateOrigin } from "./codex-lb";

const origin = "https://gateway.example";
function model(id = "gpt-synthetic", api = "openai-codex-responses", basePath = "/backend-api/codex") {
  return { id, name: id, api, basePath, reasoning: false, input: ["text"], contextWindow: 128000, maxTokens: 16384 };
}
function manifest() {
  return { schemaVersion: 1, models: [
    { ...model(), reasoning: true, input: ["text", "image"], thinking: { mode: "effort", efforts: ["low", "high"] }, preferWebsockets: true, remoteCompaction: { enabled: true, api: "openai-codex-responses" } },
    model("claude-synthetic", "anthropic-messages", "/"),
    model("glm-synthetic", "openai-completions", "/v1"),
  ] };
}

describe("codex-lb native model contract", () => {
  test("keeps native roots and honest per-model capabilities under one provider", () => {
    const models = materializeManifest(manifest(), origin);
    expect(models.map(row => row.baseUrl)).toEqual([origin + "/backend-api/codex", origin + "/", origin + "/v1"]);
    expect(models.map(row => row.api)).toEqual(["openai-codex-responses", "anthropic-messages", "openai-completions"]);
    expect(models[0].thinking?.efforts).toEqual(["low", "high"]);
    expect(models[1].reasoning).toBe(false);
    expect(models[1].input).toEqual(["text"]);
    expect(models[1].remoteCompaction).toBeUndefined();
    expect(models[2].remoteCompaction).toBeUndefined();
    expect(models.every(row => !Object.hasOwn(row, "headers") && !Object.hasOwn(row, "apiKey"))).toBe(true);
  });

  test("Anthropic root cannot include the native client's v1 suffix", () => {
    expect(() => validateManifest({ schemaVersion: 1, models: [model("claude-synthetic", "anthropic-messages", "/v1")] })).toThrow();
  });

  for (const mutation of [
    { basePath: "https://attacker.example/" }, { basePath: "//attacker.example/" },
    { basePath: "/v1/../evil" }, { basePath: "/v1?key=steal" }, { basePath: "/%2f%2fattacker" },
    { api: "custom-executable-api" }, { headers: { Authorization: "steal" } },
    { compat: { extraBody: { request: "untrusted" } } }, { apiKey: "!shell-command" },
    { contextWindow: true }, { maxTokens: Number.POSITIVE_INFINITY }, { input: ["audio"] },
    { reasoning: "true" }, { thinking: { mode: "budget", efforts: [] } },
    { remoteCompaction: { endpoint: "https://attacker.example/compact" } },
  ]) {
    test(`rejects malicious metadata ${JSON.stringify(mutation)}`, () => {
      const payload = { schemaVersion: 1, models: [{ ...model(), ...mutation }] };
      expect(() => validateManifest(payload)).toThrow();
    });
  }

  test("rejects duplicate ids and foreign schema versions", () => {
    expect(() => validateManifest({ schemaVersion: 1, models: [model(), model()] })).toThrow();
    expect(() => validateManifest({ schemaVersion: 2, models: [model()] })).toThrow();
    expect(() => validateManifest({ schemaVersion: true, models: [model()] })).toThrow();
  });

  test("accepts an honest key-scoped empty roster without invented models", () => {
    expect(materializeManifest({ schemaVersion: 1, models: [] }, origin)).toEqual([]);
  });

  test("never sends missing or empty environment-reference text to discovery", async () => {
    let called = false;
    const request = (async () => { called = true; return Response.json(manifest()); }) as unknown as typeof fetch;
    for (const key of ["", " ", "bad\nkey"]) await expect(discoverModels(origin, key, request)).rejects.toThrow();
    expect(called).toBe(false);
  });

  test("discovery fails instead of returning a destructive empty roster", async () => {
    const unavailable = (async () => new Response("server failure", { status: 503 })) as unknown as typeof fetch;
    await expect(discoverModels(origin, "synthetic-key", unavailable)).rejects.toThrow("static fallback retained");
    const malicious = (async () => Response.json({ schemaVersion: 1, models: [{ ...model(), basePath: "//attacker.example" }] })) as unknown as typeof fetch;
    await expect(discoverModels(origin, "synthetic-key", malicious)).rejects.toThrow();
  });

  test("discovery uses same-origin TLS and refuses redirects", async () => {
    const requests: Array<{ url: string; options: RequestInit }> = [];
    const request = (async (url: string, options: RequestInit) => {
      requests.push({ url, options });
      return Response.json(manifest());
    }) as unknown as typeof fetch;
    const rows = await discoverModels(origin, "synthetic-key", request);
    expect(rows).toHaveLength(3);
    expect(requests[0].url).toBe(origin + "/v1/omp/models");
    expect(requests[0].options.redirect).toBe("error");
    for (const unsafe of ["http://gateway.example", "https://user:password@gateway.example", "https://gateway.example/v1", "https://gateway.example?secret=1"]) {
      expect(() => validateOrigin(unsafe)).toThrow();
    }
  });

  // Point OMP_SOURCE at the matching installed OMP source with its workspace
  // dependencies available. This exercises the official native materializer,
  // not a forwarding echo or a local reimplementation of OAuth shaping.
  test.skipIf(!process.env.OMP_SOURCE)("official materialization gives only Claude OAuth shaping and only GPT compaction", async () => {
    const module = await import(pathToFileURL(join(process.env.OMP_SOURCE!, "packages/coding-agent/src/config/custom-models.ts")).href);
    const models = materializeManifest(manifest(), origin);
    const native = models.map(row => {
      const overlay = module.buildCustomModelOverlay("codex-lb", row.baseUrl, row.api, undefined, "SYNTHETIC_KEY_ENV", undefined, undefined, undefined, undefined, row);
      return module.finalizeCustomModel(overlay, { useDefaults: true });
    });
    expect(native.map((row: { provider: string }) => row.provider)).toEqual(["codex-lb", "codex-lb", "codex-lb"]);
    expect(native[1].isOAuth).toBe(true);
    expect(native[1].api).toBe("anthropic-messages");
    expect(native[1].baseUrl).toBe(origin + "/");
    expect(native[1].reasoning).toBe(false);
    expect(native[1].input).toEqual(["text"]);
    expect(native[0].remoteCompaction.enabled).toBe(true);
    expect(native[1].remoteCompaction?.enabled).not.toBe(true);
    expect(native[2].remoteCompaction?.enabled).not.toBe(true);
  });
});

test.skipIf(!process.env.OMP_SOURCE)("real OMP partial provider registration preserves static fallbacks, overrides and unrelated models", async () => {
  const source = process.env.OMP_SOURCE!;
  const { ModelRegistry } = await import(pathToFileURL(join(source, "packages/coding-agent/src/config/model-registry.ts")).href);
  const { AuthStorage } = await import(pathToFileURL(join(source, "packages/coding-agent/src/session/auth-storage.ts")).href);
  const directory = await mkdtemp(join(tmpdir(), "codex-lb-registry-"));
  const keyEnv = "SYNTHETIC_OMP_REGISTRY_KEY";
  const previousKey = process.env[keyEnv];
  process.env[keyEnv] = "fixture-gateway-key";
  const auth = await AuthStorage.create(":memory:");
  try {
    const ids = ["gpt-synthetic", ...Array.from({ length: 7 }, (_, index) => `gpt-preserved-${index}`)];
    const staticModels = ids.map(id => {
      const { basePath, api, ...definition } = model(id);
      return definition;
    });
    const config = {
      providers: {
        "codex-lb": {
          baseUrl: origin + "/backend-api/codex", apiKey: keyEnv, api: "openai-codex-responses", models: staticModels,
          modelOverrides: { "gpt-synthetic": { contextWindow: 500000, remoteCompaction: { enabled: true, api: "openai-codex-responses" } } },
        },
        unrelated: { baseUrl: "https://unrelated.example/v1", apiKey: "fixture-unrelated-key", api: "openai-completions", models: [{ ...staticModels[0], id: "unrelated-model" }] },
      },
    };
    const configPath = join(directory, "models.yml");
    const original = JSON.stringify(config);
    await writeFile(configPath, original, { mode: 0o600 }); // JSON is valid YAML; no legacy migration path.
    const registry = new ModelRegistry(auth, configPath, {
      cacheDbPath: join(directory, "models.db"),
      fetch: async () => { throw new Error("Test forbids external network"); },
    });
    installGatewayKeyGuard(registry, keyEnv);
    // Exercise native inference credential resolution, not just discovery or
    // initialization: static models stay visible while absent env fails closed.
    delete process.env[keyEnv];
    await expect(auth.keys.get("codex-lb")).rejects.toThrow("missing or empty");
    process.env[keyEnv] = "";
    await expect(auth.keys.get("codex-lb")).rejects.toThrow("missing or empty");
    process.env[keyEnv] = "fixture-gateway-key";
    await expect(auth.keys.get("codex-lb")).resolves.not.toBe(keyEnv);
    let unavailable = false;
    // Same partial registration as the extension: no models/baseUrl/api/auth
    // override; only gateway-key reference and the official dynamic factory.
    registry.registerProvider("codex-lb", {
      apiKey: keyEnv,
      fetchDynamicModels: async () => {
        if (unavailable) throw new Error("Synthetic discovery outage");
        return materializeManifest(manifest(), origin);
      },
    }, "ext://codex-lb-preservation-test");
    for (const id of ids) expect(registry.find("codex-lb", id)).toBeDefined();
    expect(registry.find("codex-lb", "gpt-synthetic").contextWindow).toBe(500000);
    await registry.refreshRuntimeProviders("online");
    for (const id of ids) expect(registry.find("codex-lb", id)).toBeDefined();
    expect(registry.find("codex-lb", "claude-synthetic")).toMatchObject({ api: "anthropic-messages", baseUrl: origin + "/", isOAuth: true });
    expect(registry.find("codex-lb", "glm-synthetic")).toMatchObject({ api: "openai-completions", baseUrl: origin + "/v1" });
    expect(registry.find("codex-lb", "gpt-synthetic").contextWindow).toBe(500000);
    expect(registry.find("codex-lb", "claude-synthetic").remoteCompaction?.enabled).not.toBe(true);
    expect(registry.find("codex-lb", "glm-synthetic").remoteCompaction?.enabled).not.toBe(true);
    unavailable = true;
    await registry.refreshRuntimeProviders("online");
    for (const id of ids) expect(registry.find("codex-lb", id)).toBeDefined();
    expect(registry.find("codex-lb", "gpt-synthetic").contextWindow).toBe(500000);
    delete process.env[keyEnv];
    await expect(auth.keys.get("codex-lb")).rejects.toThrow("missing or empty");
    // The wrapper delegates every unrelated reference to OMP's own resolver.
    await expect(auth.keys.get("unrelated")).resolves.toBeDefined();
    expect(registry.find("unrelated", "unrelated-model")).toMatchObject({ api: "openai-completions", baseUrl: "https://unrelated.example/v1" });
    expect(await readFile(configPath, "utf8")).toBe(original);
  } finally {
    auth.close();
    if (previousKey === undefined) delete process.env[keyEnv]; else process.env[keyEnv] = previousKey;
    await rm(directory, { recursive: true, force: true });
  }
});

function quota(model = "gpt-synthetic", provider = "codex") {
  const now = new Date().toISOString();
  return { model, provider, generated_at: now, hidden: false, windows: [{
    window: "5h", remaining_account_equivalents: 1.4, reporting_pools: 2, unknown_pools: 0,
    remaining_percent: 70, observed_at: now, resets_at: null,
  }] };
}

async function settleQuotaIO() {
  const { promise, resolve } = Promise.withResolvers<void>();
  setImmediate(resolve);
  await promise;
}

function quotaHarness() {
  let selected = { id: "gpt-synthetic", provider: "codex-lb" };
  let tick: () => void = () => {};
  const hooks: Record<string, (event: unknown, context: ExtensionContext) => void> = {};
  let command: (args: string, context: ExtensionContext) => Promise<void>;
  const statuses: Array<string | undefined> = [];
  const widgets: Array<string[] | undefined> = [];
  const notices: string[] = [];
  let cleared = 0;
  const pending: Array<{ url: string; signal: AbortSignal; resolve: (response: Response) => void; reject: (error: Error) => void }> = [];
  const context = {
    models: { current: () => selected },
    ui: {
      setStatus: (_key: string, message: string | undefined) => { statuses.push(message); },
      setWidget: (_key: string, content: string[] | undefined) => { widgets.push(content); },
      notify: (message: string) => { notices.push(message); },
    },
    setInterval: (callback: () => void, ms: number) => { expect(ms).toBe(500); tick = callback; return 1; },
    clearTimer: () => { cleared++; },
  } as unknown as ExtensionContext;
  const pi = {
    on: (event: string, handler: (event: unknown, context: ExtensionContext) => void) => { hooks[event] = handler; },
    registerCommand: (name: string, config: { handler: typeof command }) => { expect(name).toBe("lb-quota"); command = config.handler; },
  } as unknown as ExtensionAPI;
  const request = ((url: string, options: RequestInit) => {
    const { promise, resolve, reject } = Promise.withResolvers<Response>();
    pending.push({ url, signal: options.signal as AbortSignal, resolve, reject });
    return promise;
  }) as unknown as typeof fetch;
  installQuotaDisplay(pi, origin, () => "synthetic-key", request);
  return {
    pending, statuses, widgets, notices,
    start() { hooks.session_start({}, context); },
    tick() { tick(); },
    select(id: string, provider = "codex-lb") { selected = { id, provider }; },
    turn() { hooks.turn_start({}, context); },
    shutdown() { hooks.session_shutdown({}, context); return cleared; },
    manual() { return command("", context); },
  };
}

describe("model-scoped quota contract", () => {
  test("two independent fractions are 1.4 equivalents and 70%, never 140%", () => {
    const parsed = parseQuota(quota(), "gpt-synthetic");
    expect(formatQuota(parsed)).toContain("LB Codex remaining");
    expect(formatQuota(parsed)).toContain("5h 70% (1.4 pool eq; 2/2 reporting, 0 unknown)");
    expect(formatQuota(parsed)).toContain("7d unknown");
    expect(formatQuota(parsed)).not.toContain("140%");
  });

  test("partial coverage hides percent while preserving lower-bound equivalents and observed zero", () => {
    const partial = quota("claude-synthetic", "claude");
    partial.windows[0] = { ...partial.windows[0], unknown_pools: 1, remaining_percent: null as unknown as number };
    expect(formatQuota(parseQuota(partial, partial.model))).toContain("LB Claude remaining · 5h unknown (1.4 pool eq; 2/3 reporting, 1 unknown)");
    const empty = quota();
    empty.windows[0] = { ...empty.windows[0], remaining_account_equivalents: 0, remaining_percent: 0 };
    expect(formatQuota(parseQuota(empty, empty.model))).toContain("5h 0% (0 pool eq");
    const unknown = { ...quota(), windows: [{ window: "5h", remaining_account_equivalents: null, reporting_pools: 0, unknown_pools: 2, remaining_percent: null, observed_at: null, resets_at: null }] };
    expect(formatQuota(parseQuota(unknown, unknown.model))).toContain("5h unknown (unknown pool eq; 0/2 reporting, 2 unknown)");
  });

  test("rejects mismatched model, forged completion, nonfinite numbers and stale observations", () => {
    expect(() => parseQuota(quota(), "claude-synthetic")).toThrow();
    for (const patch of [
      { unknown_pools: 1 }, { remaining_percent: 140 }, { remaining_percent: 80 },
      { remaining_account_equivalents: Number.NaN }, { remaining_account_equivalents: 3 },
      { reporting_pools: true }, { reporting_pools: -1 }, { observed_at: "not a date" },
      { observed_at: new Date(Date.now() - 301_000).toISOString() },
      { resets_at: new Date(Date.now() - 1_000).toISOString() },
    ]) {
      const payload = quota();
      expect(() => parseQuota({ ...payload, windows: [{ ...payload.windows[0], ...patch }] }, payload.model)).toThrow();
    }
    const payload = quota();
    expect(() => parseQuota({ ...payload, windows: [...payload.windows, ...payload.windows] }, payload.model)).toThrow();
    expect(() => parseQuota({ ...payload, generated_at: new Date(Date.now() - 301_000).toISOString() }, payload.model)).toThrow();
    expect(() => parseQuota({ ...payload, provider: "other" }, payload.model)).toThrow();
    expect(() => parseQuota({ ...payload, hidden: true }, payload.model)).toThrow();
    expect(formatQuota(parseQuota({ ...payload, hidden: true, windows: [] }, payload.model))).toBe("LB Codex quota hidden");
    expect(formatQuota(parseQuota({ ...payload, provider: null, windows: [] }, payload.model))).toContain("no subscription quota");
  });

  test("quota authenticates exact encoded model at same TLS origin without redirects or cache", async () => {
    let calls = 0;
    const id = "alias/claude:synthetic";
    const request = (async (url: string, options: RequestInit) => {
      calls++;
      expect(url).toBe(origin + "/v1/usage/quota?model=alias%2Fclaude%3Asynthetic");
      expect(options.headers).toEqual({ Authorization: "Bearer synthetic-key", Accept: "application/json" });
      expect(options.redirect).toBe("error");
      expect(options.cache).toBe("no-store");
      expect(options.signal).toBeInstanceOf(AbortSignal);
      return Response.json(quota(id, "claude"));
    }) as unknown as typeof fetch;
    expect((await fetchQuota(origin, "synthetic-key", id, undefined, request)).provider).toBe("claude");
    for (const unsafe of ["http://gateway.example", "https://user:secret@gateway.example", "https://gateway.example/v1", "https://gateway.example?key=secret"]) {
      await expect(fetchQuota(unsafe, "synthetic-key", id, undefined, request)).rejects.toThrow("quota unavailable");
    }
    for (const key of ["", " ", "bad\nkey"]) await expect(fetchQuota(origin, key, id, undefined, request)).rejects.toThrow();
    await expect(fetchQuota(origin, "synthetic-key", "evil?token=1", undefined, request)).rejects.toThrow();
    expect(calls).toBe(1);
  });

  test("bounded response, cancellation and timeout fail closed with sanitized errors", async () => {
    for (const response of [new Response("private-provider-secret", { status: 403 }), new Response("x".repeat(65_537)), new Response("{private-provider-secret")]) {
      const request = (async () => response) as unknown as typeof fetch;
      await expect(fetchQuota(origin, "synthetic-key", "gpt-synthetic", undefined, request)).rejects.toThrow("codex-lb quota unavailable");
    }
    let calls = 0;
    const request = (async () => { calls++; throw new Error("private-key-and-url"); }) as unknown as typeof fetch;
    const cancelled = new AbortController();
    cancelled.abort();
    await expect(fetchQuota(origin, "synthetic-key", "gpt-synthetic", cancelled.signal, request)).rejects.toThrow("quota unavailable");
    const timeout = spyOn(AbortSignal, "timeout").mockReturnValue(cancelled.signal);
    try {
      await expect(fetchQuota(origin, "synthetic-key", "gpt-synthetic", undefined, request)).rejects.toThrow("quota unavailable");
      expect(calls).toBe(0);
    } finally { timeout.mockRestore(); }
    await expect(fetchQuota(origin, "synthetic-key", "gpt-synthetic", undefined, request)).rejects.toThrow("quota unavailable");
  });
});

describe("official quota UI lifecycle", () => {
  test("idle model switch aborts and discards a late previous-family response", async () => {
    const h = quotaHarness();
    h.start();
    h.select("claude-synthetic");
    h.tick();
    expect(h.pending[0].signal.aborted).toBe(true);
    expect(h.pending[1].url).toEndWith("model=claude-synthetic");
    h.pending[1].resolve(Response.json(quota("claude-synthetic", "claude")));
    await settleQuotaIO();
    expect(h.statuses.at(-1)).toContain("LB Claude");
    expect(h.statuses.at(-1)).toBe("LB Claude · 5h 70% · 7d unknown");
    expect(h.widgets.at(-1)).toBeUndefined();
    h.pending[0].resolve(Response.json(quota()));
    await settleQuotaIO();
    expect(h.statuses.at(-1)).toContain("LB Claude");
    expect(h.statuses.some(value => value?.includes("LB Codex"))).toBe(false);
    h.shutdown();
  });

  test("live selection is rechecked on IO completion before the next polling tick", async () => {
    const h = quotaHarness();
    h.start();
    h.select("claude-synthetic");
    h.pending[0].resolve(Response.json(quota()));
    await settleQuotaIO();
    expect(h.statuses.some(value => value?.includes("LB Codex"))).toBe(false);
    expect(h.pending[1].url).toEndWith("model=claude-synthetic");
    h.select("unrelated", "other-provider");
    h.tick();
    expect(h.pending[1].signal.aborted).toBe(true);
    expect(h.statuses.at(-1)).toBeUndefined();
    expect(h.widgets.at(-1)).toBeUndefined();
    h.shutdown();
  });

  test("turn hooks reuse same-model IO; manual cancellation and errors cannot restore stale quota", async () => {
    const h = quotaHarness();
    h.start();
    h.turn();
    expect(h.pending).toHaveLength(1);
    expect(h.pending[0].signal.aborted).toBe(false);
    const manual = h.manual();
    expect(h.pending[0].signal.aborted).toBe(true);
    h.pending[1].reject(new Error("secret-key URL raw transport failure"));
    await manual;
    expect(h.statuses.at(-1)).toBe("LB quota unavailable");
    h.pending[0].resolve(Response.json(quota()));
    await settleQuotaIO();
    expect(h.statuses.at(-1)).toBe("LB quota unavailable");
    expect(h.statuses.join(" ")).not.toContain("secret");
    h.shutdown();
  });

  test("manual refresh reports quota, shutdown cancels request and clears both official UI slots", async () => {
    const h = quotaHarness();
    h.start();
    const manual = h.manual();
    expect(h.pending[0].signal.aborted).toBe(true);
    h.pending[1].resolve(Response.json(quota()));
    await manual;
    expect(h.notices.at(-1)).toContain("LB Codex remaining");
    h.turn();
    expect(h.shutdown()).toBe(1);
    expect(h.pending[2].signal.aborted).toBe(true);
    h.pending[2].resolve(Response.json(quota()));
    await settleQuotaIO();
    expect(h.statuses.at(-1)).toBeUndefined();
    expect(h.widgets.at(-1)).toBeUndefined();
    h.tick();
    expect(h.pending).toHaveLength(3);
  });
});

test("quota rejects redirected/cross-origin responses and cancels an oversized stream", async () => {
  for (const field of ["redirected", "url"]) {
    const response = Response.json(quota());
    Object.defineProperty(response, field, { value: field === "redirected" ? true : "https://attacker.example/quota" });
    const request = (async () => response) as unknown as typeof fetch;
    await expect(fetchQuota(origin, "synthetic-key", "gpt-synthetic", undefined, request)).rejects.toThrow("quota unavailable");
  }
  let cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new Uint8Array(65_537)); },
    cancel() { cancelled = true; },
  });
  const request = (async () => new Response(stream)) as unknown as typeof fetch;
  await expect(fetchQuota(origin, "synthetic-key", "gpt-synthetic", undefined, request)).rejects.toThrow("quota unavailable");
  expect(cancelled).toBe(true);
});

test("an unrelated selected provider never causes quota IO, including manual refresh", async () => {
  const h = quotaHarness();
  h.select("gpt-synthetic", "direct-provider");
  h.start();
  h.tick();
  await h.manual();
  expect(h.pending).toHaveLength(0);
  expect(h.notices.at(-1)).toContain("Select a codex-lb model");
  h.shutdown();
});

test("unconfigured standalone plugin exposes setup without registering an unusable provider", async () => {
  const previous = process.env.CODEX_LB_BASE_URL;
  const previousOmni = process.env.OMNIROUTE_BASE_URL;
  delete process.env.OMNIROUTE_BASE_URL;
  delete process.env.CODEX_LB_BASE_URL;
  const directory = await mkdtemp(join(tmpdir(), "codex-lb-unconfigured-"));
  const sdk = await import("@oh-my-pi/pi-coding-agent");
  const agentDir = spyOn(sdk, "getAgentDir").mockReturnValue(directory);
  const commands = new Map<string, any>();
  let registered = false;
  try {
    const { default: plugin } = await import("./codex-lb");
    await plugin({ on() {}, registerCommand: (name: string, command: unknown) => commands.set(name, command), registerProvider: () => { registered = true; } } as unknown as ExtensionAPI);
    const notices: string[] = [];
    await commands.get("lb-connect").handler("", { ui: { notify: (message: string) => notices.push(message) } });
    expect(registered).toBe(false);
    expect(commands.has("lb-update")).toBe(true);
    expect(notices[0]).toContain("Gateway: not configured");
    expect(notices[0]).toContain("CODEX_LB_BASE_URL");
  } finally {
    agentDir.mockRestore();
    await rm(directory, { recursive: true, force: true });
    if (previous === undefined) delete process.env.CODEX_LB_BASE_URL;
    else process.env.CODEX_LB_BASE_URL = previous;
    if (previousOmni === undefined) delete process.env.OMNIROUTE_BASE_URL;
    else process.env.OMNIROUTE_BASE_URL = previousOmni;
  }
});

test("standalone plugin refuses credential-bearing origins before provider registration", async () => {
  const previous = process.env.CODEX_LB_BASE_URL;
  process.env.CODEX_LB_BASE_URL = "https://user:secret@gateway.example";
  try {
    const { default: plugin } = await import("./codex-lb");
    await expect(plugin({ on() {}, registerCommand() {} } as unknown as ExtensionAPI)).rejects.toThrow("Invalid codex-lb");
  } finally {
    if (previous === undefined) delete process.env.CODEX_LB_BASE_URL;
    else process.env.CODEX_LB_BASE_URL = previous;
  }
});

test("native catalog only supplements trusted exact IDs and keeps observed input context", async () => {
  const previousOrigin = process.env.CODEX_LB_BASE_URL;
  const previousKey = process.env.CODEX_LB_API_KEY;
  const previousOmni = process.env.OMNIROUTE_BASE_URL;
  delete process.env.OMNIROUTE_BASE_URL;
  const directory = await mkdtemp(join(tmpdir(), "native-catalog-profile-"));
  const sdk = await import("@oh-my-pi/pi-coding-agent");
  const agentDir = spyOn(sdk, "getAgentDir").mockReturnValue(directory);
  process.env.CODEX_LB_BASE_URL = origin;
  process.env.CODEX_LB_API_KEY = "fixture-key-not-displayed";
  let provider: import("@oh-my-pi/pi-coding-agent").ProviderConfigInput | undefined;
  const request = spyOn(globalThis, "fetch").mockImplementation(async url => {
    if (String(url).endsWith("/v1/omp/models")) return Response.json({ schemaVersion: 1, models: [model("gpt-6-sol")] });
    return Response.json({ models: [
      { slug: "gpt-6.1-sol", context_window: 500000, supported_in_api: true },
      { slug: "gpt-6-sol", context_window: 272000, supported_in_api: true },
      { slug: "unknown-future-model", context_window: 1000000 },
      { slug: "gpt-6-luna", context_window: 272000, visibility: "hide" },
      { slug: "gpt-6-astra", context_window: 272000, supported_in_api: false },
    ] });
  });
  try {
    const { default: plugin } = await import("./codex-lb");
    await plugin({ on() {}, registerCommand() {}, registerProvider(_id: string, config: import("@oh-my-pi/pi-coding-agent").ProviderConfigInput) { provider = config; } } as unknown as ExtensionAPI);
    const models = await provider!.fetchDynamicModels!();
    expect(models.map(row => row.id).sort()).toEqual(["gpt-6-sol", "gpt-6.1-sol"]);
    expect(models.find(row => row.id === "gpt-6.1-sol")).toMatchObject({ contextWindow: 500000, maxTokens: 128000, preferWebsockets: false });
    expect(models.find(row => row.id === "gpt-6-sol")?.contextWindow).toBe(128000);
  } finally {
    request.mockRestore();
    agentDir.mockRestore();
    await rm(directory, { recursive: true, force: true });
    if (previousOmni === undefined) delete process.env.OMNIROUTE_BASE_URL;
    else process.env.OMNIROUTE_BASE_URL = previousOmni;
    if (previousOrigin === undefined) delete process.env.CODEX_LB_BASE_URL;
    else process.env.CODEX_LB_BASE_URL = previousOrigin;
    if (previousKey === undefined) delete process.env.CODEX_LB_API_KEY;
    else process.env.CODEX_LB_API_KEY = previousKey;
  }
});
