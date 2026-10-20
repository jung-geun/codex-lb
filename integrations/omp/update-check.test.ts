import { describe, expect, spyOn, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { compareStableVersions, fetchLatestVersion, installUpdateCheck } from "./update-check";

const endpoint = "https://api.github.com/repos/jung-geun/omp-codex-lb/releases/latest";
const trusted = "https://github.com/jung-geun/omp-codex-lb/releases/latest";
const day = 86_400_000;
const release = { tag_name: "v1.1.0", draft: false, prerelease: false };

async function harness(options: { version?: string; automatic?: boolean; request?: typeof fetch } = {}) {
  const directory = await mkdtemp(join(tmpdir(), "codex-lb-update-test-"));
  const packageFile = join(directory, "package.json");
  await writeFile(packageFile, JSON.stringify({ name: "omp-codex-lb", version: options.version ?? "1.0.0" }));
  const commands = new Map<string, { handler: (args: string, context: ExtensionContext) => Promise<void> }>();
  const notices: Array<{ message: string; level: string }> = [];
  const noticeWaiters: Array<() => void> = [];
  let start!: (event: unknown, context: ExtensionContext) => unknown;
  let time = 2 * day;
  let count = 0;
  const context = { hasUI: true, ui: { notify: (message: string, level: string) => {
    notices.push({ message, level });
    noticeWaiters.shift()?.();
  } } } as unknown as ExtensionContext;
  const pi = {
    on(event: string, handler: typeof start) { expect(event).toBe("session_start"); start = handler; },
    registerCommand(name: string, command: { handler: (args: string, context: ExtensionContext) => Promise<void> }) { commands.set(name, command); },
  } as unknown as ExtensionAPI;
  const settings = {
    agentDir: directory, packageFile, now: () => time, automatic: options.automatic,
    request: (async (...args: Parameters<typeof fetch>) => {
      count++;
      return options.request ? options.request(...args) : Response.json(release);
    }) as typeof fetch,
  };
  installUpdateCheck(pi, settings);
  return {
    directory, notices, packageFile,
    get count() { return count; },
    startHeadless() { return start({}, { ...context, hasUI: false }); },
    start() { return start({}, context); },
    manual() { return commands.get("lb-update")!.handler("", context); },
    advance(delta: number) { time += delta; },
    reinstall() { installUpdateCheck(pi, settings); },
    nextNotice() {
      const { promise, resolve } = Promise.withResolvers<void>();
      noticeWaiters.push(resolve);
      return promise;
    },
    async cleanup() { await rm(directory, { recursive: true, force: true }); },
  };
}

describe("stable GitHub release comparison", () => {
  for (const [installed, latest, result] of [
    ["1.9.9", "v1.10.0", 1], ["1.99.99", "v2.0.0", 1], ["1.0.9", "v1.0.10", 1],
    ["1.2.3", "v1.2.3", 0], ["2.0.0", "v1.99.99", -1], ["1.10.0", "v1.9.99", -1],
  ] as const) {
    test(`${installed} vs ${latest}`, () => { expect(compareStableVersions(installed, latest)).toBe(result); });
  }
  for (const tag of ["1.2.3", "v1.2", "v01.2.3", "v1.2.3-beta", "v1.2.3+build", "v1.2.3\n", "v9007199254740992.0.0", "v1.2.3\u001b[31m"]) {
    test(`rejects nonstable or hostile tag ${JSON.stringify(tag)}`, () => { expect(() => compareStableVersions("1.0.0", tag)).toThrow(); });
  }
});

test("public request uses only fixed endpoint, omits credentials and refuses redirects", async () => {
  const request = (async (url: string, init: RequestInit) => {
    expect(url).toBe(endpoint);
    expect(init.redirect).toBe("error");
    expect(init.credentials).toBe("omit");
    expect(Object.fromEntries(new Headers(init.headers))).toEqual({ accept: "application/vnd.github+json" });
    expect(init.signal).toBeInstanceOf(AbortSignal);
    return Response.json({ ...release, name: "HOSTILE TITLE", body: "HOSTILE BODY", html_url: "https://evil.example/install" });
  }) as unknown as typeof fetch;
  expect(await fetchLatestVersion(request)).toBe("v1.1.0");
});

for (const body of [null, [], {}, { ...release, draft: true }, { ...release, prerelease: true }, { ...release, tag_name: "v1.1.0\nrun evil" }, { ...release, tag_name: 123 }]) {
  test(`rejects malformed or unstable release ${JSON.stringify(body)}`, async () => {
    await expect(fetchLatestVersion((async () => Response.json(body)) as typeof fetch)).rejects.toThrow();
  });
}

test("rejects HTTP failures and malformed JSON", async () => {
  await expect(fetchLatestVersion((async () => new Response("private error", { status: 403 })) as typeof fetch)).rejects.toThrow();
  await expect(fetchLatestVersion((async () => new Response("not json")) as typeof fetch)).rejects.toThrow();
});

test("enforces 64KiB while streaming even with a false Content-Length", async () => {
  let canceled = false;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new Uint8Array(32_768)); controller.enqueue(new Uint8Array(32_769)); },
    cancel() { canceled = true; },
  });
  await expect(fetchLatestVersion((async () => new Response(stream, { headers: { "Content-Length": "1" } })) as typeof fetch)).rejects.toThrow("too large");
  expect(canceled).toBe(true);
  await expect(fetchLatestVersion((async () => new Response("{}", { headers: { "Content-Length": "65537" } })) as typeof fetch)).rejects.toThrow("too large");
});

test("five second deadline covers a hanging fetch and a hanging body", async () => {
  const controller = new AbortController();
  const timeout = spyOn(AbortSignal, "timeout").mockImplementation(milliseconds => {
    expect(milliseconds).toBe(5_000);
    return controller.signal;
  });
  try {
    const pending = fetchLatestVersion((() => Promise.withResolvers<Response>().promise) as unknown as typeof fetch);
    controller.abort();
    await expect(pending).rejects.toThrow("timed out");
  } finally { timeout.mockRestore(); }
  const bodyController = new AbortController();
  let cancel = false;
  const bodyTimeout = spyOn(AbortSignal, "timeout").mockReturnValue(bodyController.signal);
  try {
    const pending = fetchLatestVersion((async () => new Response(new ReadableStream({
      start() { queueMicrotask(() => bodyController.abort()); },
      cancel() { cancel = true; },
    }))) as typeof fetch);
    await expect(pending).rejects.toThrow("timed out");
    expect(cancel).toBe(true);
  } finally { bodyTimeout.mockRestore(); }
});

test("manual update notifies with trusted text only and never installs anything", async () => {
  const h = await harness({ automatic: false, request: (async () => Response.json({ ...release, name: "EVIL", body: "SHELL", html_url: "https://evil.example/" })) as typeof fetch });
  try {
    await h.manual();
    expect(h.notices).toHaveLength(1);
    expect(h.notices[0].message).toContain("1.0.0 → v1.1.0");
    expect(h.notices[0].message).toContain(trusted);
    expect(h.notices[0].message).toContain("restart OMP");
    expect(h.notices[0].message).toContain("run omp plugin upgrade omp-codex-lb and restart OMP");
    expect(h.notices[0].message).not.toMatch(/EVIL|SHELL|evil\.example/);
  } finally { await h.cleanup(); }
});

test("headless sessions do not consume the interactive notification throttle", async () => {
  const h = await harness({ automatic: true });
  try {
    h.startHeadless();
    expect(h.count).toBe(0);
    await expect(readFile(join(h.directory, "codex-lb-update-check.json"), "utf8")).rejects.toThrow();
    const notice = h.nextNotice();
    h.start();
    await notice;
    expect(h.count).toBe(1);
  } finally { await h.cleanup(); }
});

test("automatic check returns immediately and persists a daily throttle across installations", async () => {
  const h = await harness({ automatic: true });
  try {
    const firstNotice = h.nextNotice();
    expect(h.start()).toBeUndefined();
    h.start();
    await firstNotice;
    expect(h.count).toBe(1);
    expect(JSON.parse(await readFile(join(h.directory, "codex-lb-update-check.json"), "utf8"))).toEqual({ lastCheckedAt: 2 * day });
    h.reinstall();
    h.start();
    // Explicit manual checks bypass the state and wait for a background check.
    await h.manual();
    expect(h.count).toBe(2);
    h.advance(day - 1);
    h.start();
    await h.manual();
    expect(h.count).toBe(3);
    h.advance(day);
    const nextNotice = h.nextNotice();
    h.start();
    await nextNotice;
    expect(h.count).toBe(4);
    expect(h.notices).toHaveLength(4);
  } finally { await h.cleanup(); }
});

test("automatic same-version and downgrade checks show no update notice", async () => {
  for (const version of ["1.1.0", "2.0.0"]) {
    const h = await harness({ automatic: true, version });
    try {
      h.start();
      await h.manual();
      expect(h.count).toBe(2);
      expect(h.notices).toHaveLength(1);
      expect(h.notices[0].message).not.toContain("update available");
      expect(h.notices[0].message).toContain(version === "1.1.0" ? "up to date" : "installed version is newer");
    } finally { await h.cleanup(); }
  }
});

test("automatic failures are silent, throttled, and manual failures use no remote error text", async () => {
  const h = await harness({ automatic: true, request: (async () => { throw new Error("SECRET gateway key or URL"); }) as typeof fetch });
  try {
    h.start();
    await h.manual();
    expect(h.notices).toHaveLength(1);
    expect(h.notices[0].level).toBe("warning");
    expect(h.notices[0].message).toContain("check failed (installed 1.0.0)");
    expect(h.notices[0].message).not.toContain("SECRET");
    h.start();
    await h.manual();
    expect(h.count).toBe(3);
  } finally { await h.cleanup(); }
});

test("environment opt out disables background check but explicit command still works", async () => {
  const previous = process.env.CODEX_LB_UPDATE_CHECK;
  process.env.CODEX_LB_UPDATE_CHECK = "0";
  const h = await harness();
  try {
    h.start();
    expect(h.count).toBe(0);
    await h.manual();
    expect(h.count).toBe(1);
    expect(h.notices[0].message).toContain("update available");
  } finally {
    if (previous === undefined) delete process.env.CODEX_LB_UPDATE_CHECK;
    else process.env.CODEX_LB_UPDATE_CHECK = previous;
    await h.cleanup();
  }
});

test("missing package metadata never invents an installed version", async () => {
  const h = await harness({ automatic: true });
  try {
    await rm(h.packageFile);
    h.start();
    await h.manual();
    expect(h.count).toBe(0);
    expect(h.notices).toHaveLength(1);
    expect(h.notices[0].message).toContain("installed package version unavailable");
  } finally { await h.cleanup(); }
});

test("foreign package metadata cannot supply the installed plugin version", async () => {
  const h = await harness({ automatic: true });
  try {
    await writeFile(h.packageFile, JSON.stringify({ name: "another-extension", version: "1.0.0" }));
    h.start();
    await h.manual();
    expect(h.count).toBe(0);
    expect(h.notices[0].message).toContain("installed package version unavailable");
  } finally { await h.cleanup(); }
});

test("future state timestamps do not suppress checks after clock correction", async () => {
  const h = await harness({ automatic: true });
  try {
    await writeFile(join(h.directory, "codex-lb-update-check.json"), JSON.stringify({ lastCheckedAt: 3 * day }));
    h.start();
    await h.manual();
    expect(h.count).toBe(2);
    expect(h.notices).toHaveLength(2);
  } finally { await h.cleanup(); }
});
