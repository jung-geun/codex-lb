import { createHash } from "node:crypto";

/** Stable provider-proven principal, not an email, display name, or rotating token. */
export function quotaCapacityIdentity(provider: string, value: unknown): string | null {
  let data: Record<string, unknown>;
  try {
    const parsed = typeof value === "string" ? JSON.parse(value) : value;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    data = parsed as Record<string, unknown>;
  } catch { return null; }
  const fields = provider === "codex" ? [data.workspaceId, data.chatgptUserId]
    : provider === "claude" ? [data.organizationUUID, data.accountUUID] : [];
  if (!fields.length || fields.some((field) => typeof field !== "string" || !field.trim())) return null;
  return createHash("sha256").update(JSON.stringify([provider, ...fields])).digest("hex");
}
