import assert from "node:assert/strict";
import test from "node:test";
import { classifyRoute } from "../../../src/server/authz/classify";

test("OMP Codex native alias shares client API authorization for base and child requests", () => {
  for (const prefix of ["/backend-api/codex/responses", "/BACKEND-API/CODEX/RESPONSES"]) {
    for (const tail of ["", "/", "/input_tokens", "/compact"]) {
      const route = classifyRoute(prefix + tail, "POST");
      assert.equal(route.routeClass, "CLIENT_API");
      assert.equal(route.normalizedPath, "/api/v1/responses" + tail.replace(/\/$/, ""));
    }
  }
});

test("lookalike native aliases do not acquire the inference authorization class", () => {
  for (const path of [
    "/backend-api/codex/responses-evil",
    "/backend-api/codexx/responses",
    "/backend-api/codex/models",
  ]) {
    assert.equal(classifyRoute(path, "POST").routeClass, "MANAGEMENT");
  }
});
