import assert from "node:assert/strict";
import test from "node:test";
import { quotaCapacityIdentity } from "../../src/lib/quota/capacityIdentity.ts";

test("Codex capacity is seat-scoped rather than workspace or email scoped", () => {
  const first = quotaCapacityIdentity("codex", { workspaceId: "team", chatgptUserId: "seat-a" });
  assert.notEqual(first, quotaCapacityIdentity("codex", { workspaceId: "team", chatgptUserId: "seat-b" }));
  assert.equal(first, quotaCapacityIdentity("codex", { workspaceId: "team", chatgptUserId: "seat-a", email: "changed@example.invalid" }));
  assert.equal(quotaCapacityIdentity("codex", { workspaceId: "team", email: "same@example.invalid" }), null);
});

test("Claude capacity cannot cross organization or unproven account identity", () => {
  const first = quotaCapacityIdentity("claude", { accountUUID: "account", organizationUUID: "org-a" });
  assert.notEqual(first, quotaCapacityIdentity("claude", { accountUUID: "account", organizationUUID: "org-b" }));
  assert.equal(quotaCapacityIdentity("claude", { email: "same@example.invalid" }), null);
  assert.equal(quotaCapacityIdentity("claude", "invalid-json"), null);
});
