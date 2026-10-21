import { expect, spyOn, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import * as sdk from "@oh-my-pi/pi-coding-agent";
import type { ExtensionAPI, ExtensionContext, ProviderConfigInput } from "@oh-my-pi/pi-coding-agent";
import { complete } from "@oh-my-pi/pi-ai";
import type { Context, FetchImpl, Tool } from "@oh-my-pi/pi-ai";
import plugin, { installGatewayKeyGuard, installQuotaDisplay } from "./codex-lb";

test("both gateway credentials fail closed independently after both guards are installed", async () => {
  const names = ["SYNTHETIC_CODEX_GUARD_KEY", "SYNTHETIC_OMNIROUTE_GUARD_KEY"];
  const previous = names.map(name => process.env[name]);
  const directory = await mkdtemp(join(tmpdir(), "omp-dual-gateway-"));
  const auth = await AuthStorage.create(":memory:");
  try {
    const registry = new ModelRegistry(auth, join(directory, "models.yml"), { cacheDbPath: join(directory, "models.db") });
    for (const [index, provider] of ["codex-lb", "omniroute"].entries()) {
      registry.registerProvider(provider, { apiKey: names[index] }, `ext://dual-${provider}`);
      installGatewayKeyGuard(registry, names[index]);
    }
    process.env[names[0]] = "synthetic-codex-secret";
    process.env[names[1]] = "synthetic-omniroute-secret";
    expect(await auth.keys.get("codex-lb")).toBe("synthetic-codex-secret");
    expect(await auth.keys.get("omniroute")).toBe("synthetic-omniroute-secret");
    delete process.env[names[0]];
    await expect(auth.keys.get("codex-lb")).rejects.toThrow();
    expect(await auth.keys.get("omniroute")).toBe("synthetic-omniroute-secret");
    process.env[names[0]] = "synthetic-codex-secret";
    process.env[names[1]] = "";
    await expect(auth.keys.get("omniroute")).rejects.toThrow();
    expect(await auth.keys.get("codex-lb")).toBe("synthetic-codex-secret");
  } finally {
    auth.close();
    await rm(directory, { recursive: true, force: true });
    for (const [index, name] of names.entries()) {
      if (previous[index] === undefined) delete process.env[name];
      else process.env[name] = previous[index];
    }
  }
});

test("switching the same model between gateways cannot restore another gateway's late quota", async () => {
  let selected = { id: "gpt-synthetic", provider: "codex-lb" };
  const hooks = new Map<string, Array<(event: unknown, context: ExtensionContext) => void>>();
  const ticks = new Set<() => void>();
  const statuses = new Map<string, string | undefined>();
  const pending: Array<{ signal: AbortSignal; resolve: (response: Response) => void }> = [];
  const context = {
    models: { current: () => selected },
    ui: {
      setStatus: (key: string, value: string | undefined) => statuses.set(key, value),
      setWidget() {}, notify() {},
    },
    setInterval(callback: () => void) { ticks.add(callback); return callback; },
    clearTimer(callback: () => void) { ticks.delete(callback); },
  } as unknown as ExtensionContext;
  const pi = {
    on(event: string, handler: (event: unknown, context: ExtensionContext) => void) {
      hooks.set(event, [...hooks.get(event) ?? [], handler]);
    },
    registerCommand() {},
  } as unknown as ExtensionAPI;
  const request = ((_url: string, init: RequestInit) => {
    const { promise, resolve } = Promise.withResolvers<Response>();
    pending.push({ signal: init.signal as AbortSignal, resolve });
    return promise;
  }) as unknown as typeof fetch;
  for (const provider of ["codex-lb", "omniroute"] as const) {
    installQuotaDisplay(pi, `https://${provider}.example`, () => "synthetic-key", request, provider);
  }
  const emit = (event: string) => hooks.get(event)?.forEach(handler => handler({}, context));
  const settle = () => new Promise<void>(resolve => setImmediate(resolve));
  const response = (percent: number) => {
    const now = new Date().toISOString();
    return Response.json({ model: selected.id, provider: "codex", generated_at: now, hidden: false,
      windows: [{ window: "5h", remaining_account_equivalents: percent / 100, reporting_pools: 1,
        unknown_pools: 0, remaining_percent: percent, observed_at: now, resets_at: null }] });
  };
  try {
    emit("session_start");
    expect(pending).toHaveLength(1);
    selected = { ...selected, provider: "omniroute" };
    ticks.forEach(tick => tick());
    expect(pending[0].signal.aborted).toBe(true);
    pending[1].resolve(response(42));
    await settle();
    expect(statuses.get("omniroute-quota")).toContain("42%");
    expect(statuses.get("codex-lb-quota")).toBeUndefined();
    pending[0].resolve(response(70));
    await settle();
    expect(statuses.get("omniroute-quota")).toContain("42%");
    expect(statuses.get("codex-lb-quota")).toBeUndefined();
    selected = { ...selected, provider: "codex-lb" };
    ticks.forEach(tick => tick());
    pending[2].resolve(response(23));
    await settle();
    expect(statuses.get("codex-lb-quota")).toContain("23%");
    expect(statuses.get("omniroute-quota")).toBeUndefined();
  } finally {
    emit("session_shutdown");
  }
  expect(ticks.size).toBe(0);
  expect([...statuses.values()].every(value => value === undefined)).toBe(true);
});

const gatewayOrigin = "https://omniroute.example";
function gatewayModel(id: string, api = "openai-codex-responses", basePath = "/backend-api/codex") {
  return { id, name: id, api, basePath, reasoning: false, input: ["text"], contextWindow: 128000, maxTokens: 16384 };
}
function geminiModel() {
  return {
    ...gatewayModel("antigravity/gemini-2.5-flash", "openai-completions", "/v1"),
    input: ["text", "image"], contextWindow: 1048576, maxTokens: 65536,
    compat: { maxTokensField: "max_tokens", supportsDeveloperRole: false, supportsStore: false, supportsReasoningEffort: false },
  };
}

// Exercise the plugin's actual factory through the host registry, with only
// discovery HTTP replaced. Config and cache live in an isolated temporary dir.
async function withGatewayRegistry(
  provider: "codex-lb" | "omniroute",
  run: (fixture: {
    registry: ModelRegistry;
    auth: AuthStorage;
    config: ProviderConfigInput;
    requests: string[];
    respond: (handler: () => Response) => void;
  }) => Promise<void>,
) {
  const names = ["CODEX_LB_BASE_URL", "CODEX_LB_API_KEY", "CODEX_LB_KEY_ENV", "OMNIROUTE_BASE_URL", "OMNIROUTE_API_KEY", "OMNIROUTE_KEY_ENV"];
  const previous = names.map(name => process.env[name]);
  const directory = await mkdtemp(join(tmpdir(), "omp-authoritative-gateway-"));
  const agentDir = spyOn(sdk, "getAgentDir").mockReturnValue(directory);
  const auth = await AuthStorage.create(":memory:");
  const requests: string[] = [];
  let response = () => Response.json({ schemaVersion: 1, models: [] });
  const request = spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
    requests.push(String(url));
    expect(new Headers(init?.headers).get("Authorization")).toBe(["Bearer", "synthetic-gateway-key"].join(" "));
    if (String(url) === gatewayOrigin + "/v1/omp/models") return response();
    if (String(url) === gatewayOrigin + "/backend-api/codex/models") {
      return Response.json({ models: [{ slug: "gpt-6.1-sol", context_window: 500000, supported_in_api: true }] });
    }
    throw new Error("Unexpected discovery request");
  });
  try {
    for (const name of names) delete process.env[name];
    const environment = provider === "omniroute" ? "OMNIROUTE" : "CODEX_LB";
    process.env[`${environment}_BASE_URL`] = gatewayOrigin;
    process.env[`${environment}_API_KEY`] = "synthetic-gateway-key";
    const { basePath, ...staticModel } = gatewayModel("gpt-static-only");
    const configPath = join(directory, "models.yml");
    const original = JSON.stringify({ providers: {
      [provider]: { baseUrl: gatewayOrigin + basePath, api: staticModel.api, apiKey: `${environment}_API_KEY`, models: [staticModel] },
      unrelated: { baseUrl: "https://unrelated.example/v1", api: "openai-completions", apiKey: "synthetic-unrelated-key", models: [{ ...staticModel, id: "unrelated-model", api: "openai-completions" }] },
    } });
    await writeFile(configPath, original, { mode: 0o600 });
    const registry = new ModelRegistry(auth, configPath, { cacheDbPath: join(directory, "models.db") });
    let registered: ProviderConfigInput | undefined;
    await plugin({
      on() {}, registerCommand() {},
      registerProvider(id: string, config: ProviderConfigInput) {
        if (id === provider) registered = config;
        registry.registerProvider(id, config, `ext://gateway-${id}`);
      },
    } as unknown as ExtensionAPI);
    installGatewayKeyGuard(registry, `${environment}_API_KEY`);
    expect(registered).toBeDefined();
    await run({ registry, auth, config: registered!, requests, respond: handler => { response = handler; } });
    expect(registry.find("unrelated", "unrelated-model")).toBeDefined();
    expect(await readFile(configPath, "utf8")).toBe(original);
  } finally {
    request.mockRestore();
    agentDir.mockRestore();
    auth.close();
    await rm(directory, { recursive: true, force: true });
    for (const [index, name] of names.entries()) {
      if (previous[index] === undefined) delete process.env[name];
      else process.env[name] = previous[index];
    }
  }
}

test("OmniRoute manifest prunes cached discovery exclusions without a native Codex union; YAML static overlays remain", async () => {
  await withGatewayRegistry("omniroute", async ({ registry, requests, respond }) => {
    const claude = gatewayModel("claude-sonnet-4-5", "anthropic-messages", "/");
    respond(() => Response.json({ schemaVersion: 1, models: [gatewayModel("gpt-6.1-sol"), claude] }));
    await registry.refreshRuntimeProviders("online");
    expect(registry.getProviderModels("omniroute").map(model => model.id).sort()).toEqual(["claude-sonnet-4-5", "gpt-6.1-sol", "gpt-static-only"]);
    respond(() => Response.json({ schemaVersion: 1, models: [geminiModel(), claude] }));
    await registry.refreshRuntimeProviders("online");
    expect(registry.getProviderModels("omniroute").map(model => model.id).sort()).toEqual(["antigravity/gemini-2.5-flash", "claude-sonnet-4-5", "gpt-static-only"]);
    expect(registry.find("omniroute", "gpt-6.1-sol")).toBeUndefined();
    // The host reapplies explicit models.yml overlays after discovery. They
    // are not pruned by the plugin's authoritative manifest or native cache.
    expect(registry.find("omniroute", "gpt-static-only")).toBeDefined();
    expect(registry.find("omniroute", claude.id)).toMatchObject({ api: "anthropic-messages", baseUrl: gatewayOrigin + "/", isOAuth: true });
    expect(registry.find("omniroute", geminiModel().id)).toMatchObject({
      api: "openai-completions", baseUrl: gatewayOrigin + "/v1", contextWindow: 1048576,
      maxTokens: 65536, input: ["text", "image"], reasoning: false,
    });
    expect(requests).toEqual([gatewayOrigin + "/v1/omp/models", gatewayOrigin + "/v1/omp/models"]);
  });
});

test("OmniRoute empty success removes cached discovery, not explicit YAML static overlays, and never invokes native fallback", async () => {
  await withGatewayRegistry("omniroute", async ({ registry, config, requests, respond }) => {
    respond(() => Response.json({ schemaVersion: 1, models: [gatewayModel("gpt-6.1-sol"), geminiModel()] }));
    await registry.refreshRuntimeProviders("online");
    expect(registry.getProviderModels("omniroute")).toHaveLength(3);
    respond(() => Response.json({ schemaVersion: 1, models: [] }));
    await expect(config.fetchDynamicModels!(undefined)).resolves.toEqual([]);
    await registry.refreshRuntimeProviders("online");
    expect(registry.getProviderModels("omniroute").map(model => model.id)).toEqual(["gpt-static-only"]);
    expect(registry.getProviderDiscoveryState("omniroute")).toMatchObject({ status: "empty", stale: false, models: [] });
    expect(requests.every(url => url === gatewayOrigin + "/v1/omp/models")).toBe(true);
  });
});

for (const failure of ["unauthorized", "unavailable", "invalid-contract"] as const) {
  test(`OmniRoute ${failure} is a discovery error, not an authoritative empty success or native fallback`, async () => {
    await withGatewayRegistry("omniroute", async ({ registry, config, requests, respond }) => {
      const failedResponse = () => failure === "invalid-contract"
        ? Response.json({ schemaVersion: 2, models: [] })
        : new Response("Not a model manifest", { status: failure === "unauthorized" ? 401 : 503 });
      respond(failedResponse);
      await expect(config.fetchDynamicModels!(undefined)).rejects.toThrow("omniroute discovery failed");
      await registry.refreshRuntimeProviders("online");
      expect(registry.find("omniroute", "gpt-static-only")).toBeDefined();
      expect(registry.getProviderDiscoveryState("omniroute")?.stale).toBe(true);
      respond(() => Response.json({ schemaVersion: 1, models: [geminiModel()] }));
      await registry.refreshRuntimeProviders("online");
      respond(failedResponse);
      await registry.refreshRuntimeProviders("online");
      // A failed refresh retains the host's last-known catalog; it does not
      // claim the server granted access or silently manufacture a new roster.
      expect(registry.find("omniroute", geminiModel().id)).toBeDefined();
      expect(registry.getProviderDiscoveryState("omniroute")?.stale).toBe(true);
      expect(requests.every(url => url === gatewayOrigin + "/v1/omp/models")).toBe(true);
    });
  });
}

test("legacy codex-lb still discovers trusted native IDs when its manifest is unavailable", async () => {
  await withGatewayRegistry("codex-lb", async ({ registry, requests, respond }) => {
    respond(() => new Response("Legacy gateway", { status: 404 }));
    await registry.refreshRuntimeProviders("online");
    expect(registry.find("codex-lb", "gpt-6.1-sol")).toMatchObject({
      api: "openai-codex-responses", baseUrl: gatewayOrigin + "/backend-api/codex",
      contextWindow: 500000, maxTokens: 128000, preferWebsockets: false,
    });
    expect(requests.sort()).toEqual([gatewayOrigin + "/backend-api/codex/models", gatewayOrigin + "/v1/omp/models"].sort());
  });
});

test("registered Antigravity Gemini uses native Chat Completions vision, tool-call parsing and tool-result continuation", async () => {
  await withGatewayRegistry("omniroute", async ({ registry, auth, respond }) => {
    respond(() => Response.json({ schemaVersion: 1, models: [geminiModel()] }));
    await registry.refreshRuntimeProviders("online");
    const model = registry.find("omniroute", geminiModel().id)!;
    expect(model.isOAuth).not.toBe(true);
    expect(model.remoteCompaction?.enabled).not.toBe(true);
    expect(model.thinking).toBeUndefined();
    const tool: Tool = { name: "weather", description: "Look up weather", parameters: {
      type: "object", properties: { city: { type: "string" } }, required: ["city"], additionalProperties: false,
    } };
    const context: Context = { systemPrompt: "Use the provided tool.", tools: [tool], messages: [{
      role: "user", timestamp: 1, content: [
        { type: "text", text: "Read the image and get the weather in Seoul." },
        { type: "image", mimeType: "image/png", data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jCBsAAAAASUVORK5CYII=" },
      ],
    }] };
    const sent: Array<{ url: string; headers: Headers; body: Record<string, unknown> }> = [];
    const transport: FetchImpl = async (input, init) => {
      const request = new Request(input, init);
      const body: unknown = await request.json();
      if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("Expected a native JSON request object");
      sent.push({ url: request.url, headers: request.headers, body: body as Record<string, unknown> });
      // Fixed protocol responses exercise native SSE decoding, not an echo
      // server or replacement streamer. The second request must replay the
      // parsed first response and a tool result using Chat Completions roles.
      const first = sent.length === 1;
      const delta = first
        ? { role: "assistant", tool_calls: [{ index: 0, id: "call_weather", type: "function", function: { name: "weather", arguments: '{"city":"Seoul"}' } }] }
        : { role: "assistant", content: "Seoul is sunny." };
      const chunks = [
        { id: "chatcmpl-fixture", object: "chat.completion.chunk", created: 1, model: geminiModel().id, choices: [{ index: 0, delta, finish_reason: null }] },
        { id: "chatcmpl-fixture", object: "chat.completion.chunk", created: 1, model: geminiModel().id, choices: [{ index: 0, delta: {}, finish_reason: first ? "tool_calls" : "stop" }], usage: { prompt_tokens: 40, completion_tokens: 8, total_tokens: 48 } },
      ];
      return new Response(chunks.map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join("") + "data: [DONE]\n\n", {
        headers: { "Content-Type": "text/event-stream" },
      });
    };
    const options = { apiKey: await auth.keys.get("omniroute"), maxTokens: 2048, fetch: transport, maxHttpAttempts: 1 };
    const first = await complete(model, context, options);
    expect(first).toMatchObject({ provider: "omniroute", model: geminiModel().id, api: "openai-completions", stopReason: "toolUse" });
    expect(first.content).toContainEqual(expect.objectContaining({ type: "toolCall", id: "call_weather", name: "weather", arguments: { city: "Seoul" } }));
    context.messages.push(first, { role: "toolResult", toolCallId: "call_weather", toolName: "weather", content: [{ type: "text", text: "Sunny, 20C" }], isError: false, timestamp: 2 });
    const second = await complete(model, context, options);
    expect(second.stopReason).toBe("stop");
    expect(second.content).toContainEqual(expect.objectContaining({ type: "text", text: "Seoul is sunny." }));
    expect(sent).toHaveLength(2);
    for (const request of sent) {
      expect(request.url).toBe(gatewayOrigin + "/v1/chat/completions");
      expect(request.headers.get("authorization")).toBe(["Bearer", "synthetic-gateway-key"].join(" "));
      expect(request.body.model).toBe("antigravity/gemini-2.5-flash");
      expect(request.body.max_tokens).toBe(2048);
      expect(request.body.max_completion_tokens).toBeUndefined();
      expect(request.body.reasoning_effort).toBeUndefined();
      expect(request.body.tools).toContainEqual(expect.objectContaining({ type: "function", function: expect.objectContaining({ name: "weather", parameters: expect.objectContaining({ type: "object", required: ["city"] }) }) }));
      expect(request.body.messages).toContainEqual(expect.objectContaining({ role: "user", content: expect.arrayContaining([expect.objectContaining({ type: "image_url" })]) }));
    }
    expect(sent[1].body.messages).toContainEqual(expect.objectContaining({ role: "assistant", tool_calls: [expect.objectContaining({ id: "call_weather", type: "function", function: { name: "weather", arguments: '{"city":"Seoul"}' } })] }));
    expect(sent[1].body.messages).toContainEqual(expect.objectContaining({ role: "tool", tool_call_id: "call_weather", content: "Sunny, 20C" }));
  });
});
