import { expect, test } from "bun:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { installGatewayKeyGuard, installQuotaDisplay } from "./codex-lb";

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
