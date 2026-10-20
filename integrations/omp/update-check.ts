import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { getAgentDir } from "@oh-my-pi/pi-coding-agent";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const ENDPOINT = "https://api.github.com/repos/jung-geun/omp-codex-lb/releases/latest";
const RELEASES = "https://github.com/jung-geun/omp-codex-lb/releases/latest";
const DAY = 24 * 60 * 60 * 1000;
const MAX_BYTES = 64 * 1024;
const UPDATE = `Review ${RELEASES}. Git installs: run omp plugin upgrade omp-codex-lb and restart OMP; tag-pinned installs: install the reviewed new tag explicitly. Local links: update the trusted checkout and restart OMP. Snapshot installs: rerun scripts/connect-omp.py from your reviewed, updated codex-lb checkout.`;

type Version = readonly [number, number, number];
function parseVersion(value: unknown, tag: boolean): Version {
  if (typeof value !== "string" || value.trim() !== value || !(tag ? /^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/ : /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/).test(value)) {
    throw new Error("Stable version unavailable");
  }
  const parts = (tag ? value.slice(1) : value).split(".").map(Number);
  if (!parts.every(Number.isSafeInteger)) throw new Error("Stable version unavailable");
  return parts as unknown as Version;
}

export function compareStableVersions(installed: string, latestTag: string): number {
  const current = parseVersion(installed, false);
  const latest = parseVersion(latestTag, true);
  for (let i = 0; i < 3; i++) if (latest[i] !== current[i]) return latest[i] > current[i] ? 1 : -1;
  return 0;
}

export async function fetchLatestVersion(request: typeof fetch = fetch): Promise<string> {
  const signal = AbortSignal.timeout(5_000);
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  const { promise: expired, reject } = Promise.withResolvers<never>();
  const onAbort = () => {
    if (reader) void reader.cancel().catch(() => {});
    reject(new Error("Release check timed out"));
  };
  signal.addEventListener("abort", onAbort, { once: true });
  if (signal.aborted) onAbort();
  const load = async () => {
    // This is a public GitHub request: never use gateway configuration or keys.
    const response = await request(ENDPOINT, {
      headers: { Accept: "application/vnd.github+json" }, redirect: "error", credentials: "omit", signal,
    });
    if (!response.ok || !response.body) throw new Error("Release unavailable");
    const declared = response.headers.get("content-length");
    if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_BYTES)) {
      void response.body.cancel().catch(() => {});
      throw new Error("Release body too large");
    }
    reader = response.body.getReader();
    const bytes = new Uint8Array(MAX_BYTES);
    let length = 0;
    try {
      if (signal.aborted) throw new Error("Release check timed out");
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        if (length + chunk.value.length > MAX_BYTES) throw new Error("Release body too large");
        bytes.set(chunk.value, length);
        length += chunk.value.length;
      }
      const release = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, length)));
      if (!release || typeof release !== "object" || Array.isArray(release) || release.draft !== false || release.prerelease !== false) throw new Error("Stable release unavailable");
      const parts = parseVersion(release.tag_name, true);
      return `v${parts.join(".")}`;
    } finally {
      void reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  };
  try { return await Promise.race([load(), expired]); }
  finally { signal.removeEventListener("abort", onAbort); }
}

// Narrow dependency overrides keep tests off the user's profile and the network.
type UpdateCheckOptions = {
  agentDir?: string;
  packageFile?: string | URL;
  request?: typeof fetch;
  now?: () => number;
  automatic?: boolean;
};

export function installUpdateCheck(pi: ExtensionAPI, options: UpdateCheckOptions = {}): void {
  const state = join(options.agentDir ?? getAgentDir(), "codex-lb-update-check.json");
  const packageFile = options.packageFile ?? new URL("./package.json", import.meta.url);
  const now = options.now ?? Date.now;
  const automatic = options.automatic ?? process.env.CODEX_LB_UPDATE_CHECK !== "0";
  let running: Promise<void> | undefined;
  const check = async (context: ExtensionContext, manual: boolean): Promise<void> => {
    let installed: string | undefined;
    try {
      const metadata = JSON.parse(await readFile(packageFile, "utf8"));
      if (metadata.name !== "omp-codex-lb") throw new Error("Installed plugin package unavailable");
      parseVersion(metadata.version, false);
      installed = metadata.version;
      const checkedAt = now();
      if (!manual) {
        try {
          const saved = JSON.parse(await readFile(state, "utf8"));
          if (Number.isSafeInteger(saved.lastCheckedAt) && saved.lastCheckedAt >= 0 && saved.lastCheckedAt <= checkedAt && checkedAt - saved.lastCheckedAt < DAY) return;
        } catch (error) {
          if (!error || typeof error !== "object" || !("code" in error) || error.code !== "ENOENT") {
            // Corrupt state can be replaced; inaccessible state cannot be trusted.
            if (!(error instanceof SyntaxError)) throw error;
          }
        }
      }
      // Persist the attempt before networking, including failed requests.
      try { await writeFile(state, JSON.stringify({ lastCheckedAt: checkedAt }) + "\n", { mode: 0o600 }); }
      catch (error) { if (!manual) throw error; }
      const latest = await fetchLatestVersion(options.request);
      const comparison = compareStableVersions(installed!, latest);
      if (comparison > 0) context.ui.notify(`codex-lb plugin update available: ${installed} → ${latest}.\n${UPDATE}`, "info");
      else if (manual) context.ui.notify(`codex-lb plugin ${installed}: ${comparison === 0 ? "up to date" : "installed version is newer than the latest stable release"} (latest ${latest}).\n${UPDATE}`, "info");
    } catch {
      if (manual) context.ui.notify(`codex-lb plugin update check failed${installed ? ` (installed ${installed})` : ": installed package version unavailable"}. Try again later.\n${UPDATE}`, "warning");
    }
  };
  pi.registerCommand("lb-update", {
    description: "Check the public stable plugin release and show manual update instructions",
    handler: async (_args, context) => {
      if (running) await running;
      running = check(context, true);
      try { await running; } finally { running = undefined; }
    },
  });
  pi.on("session_start", (_event, context) => {
    if (!automatic || !context.hasUI || running) return;
    // Do not return this promise: startup and inference never wait for GitHub.
    running = check(context, false);
    void running.finally(() => { running = undefined; });
  });
}
