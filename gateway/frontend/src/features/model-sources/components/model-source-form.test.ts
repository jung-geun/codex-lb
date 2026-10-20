import { describe, expect, it } from "vitest";
import i18n from "@/i18n";
import { createModelSourceFormSchema, mergeReasoningMetadata, parseReasoningEffortsInput } from "./model-source-form";

describe("model-source-form reasoning effort normalization", () => {
  it("trims whitespace and preserves casing for effort values", () => {
    expect(parseReasoningEffortsInput("  Ultra,   xhigh , low  ")).toEqual([
      "Ultra",
      "xhigh",
      "low",
    ]);
  });

  it("preserves casing for declared default reasoning effort", () => {
    const metadata = mergeReasoningMetadata(
      null,
      true,
      ["Ultra", "provider-specific", "xhigh"],
      "  provider-specific  ",
    );
    const parsed = JSON.parse(metadata ?? "{}");

    expect(parsed.supported_reasoning_levels).toEqual(["Ultra", "provider-specific", "xhigh"]);
    expect(parsed.default_reasoning_level).toBe("provider-specific");
  });
});

describe("source connection model discovery", () => {
  it("allows an unverified provider to discover models later but requires custom model IDs", () => {
    const values = { name: "Provider", baseUrl: "https://api.example.com", apiKey: "", models: "" };
    expect(createModelSourceFormSchema(i18n.t, true).safeParse(values).success).toBe(true);
    expect(createModelSourceFormSchema(i18n.t).safeParse(values).success).toBe(false);
  });
});
