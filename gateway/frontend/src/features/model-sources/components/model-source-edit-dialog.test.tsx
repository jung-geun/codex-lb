import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/utils";
import type { ModelSource } from "@/features/model-sources/schemas";

import { ModelSourceEditDialog } from "./model-source-edit-dialog";

function createModelSource(overrides: Partial<ModelSource> = {}): ModelSource {
  return {
    id: "src_1",
    name: "vllm-local",
    kind: "openai_compatible",
    baseUrl: "http://127.0.0.1:8000/v1",
    isEnabled: true,
    routingPolicy: "normal",
    apiFallbackOnly: false,
    limitWarmupEnabled: false,
    limitWarmup: null,
    warmupSupported: false,
    warmupUnavailableReason: "unsupported",
    healthStatus: "unknown",
    supportsChatCompletions: true,
    supportsResponses: false,
    supportsAudioTranscriptions: false,
    supportsEmbeddings: false,
    timeoutSeconds: null,
    maxConcurrency: null,
    createdAt: "2026-07-03T00:00:00Z",
    updatedAt: "2026-07-03T00:00:00Z",
    models: [
      {
        id: 1,
        sourceId: "src_1",
        model: "Qwen/Qwen3.6-27B-FP8",
        displayName: "Qwen/Qwen3.6-27B-FP8",
        contextWindow: 32768,
        maxOutputTokens: 4096,
        supportsStreaming: true,
        supportsTools: true,
        supportsVision: false,
        inputPer1M: 0.5,
        cachedInputPer1M: null,
        outputPer1M: 1.5,
        audioPerMinute: null,
        rawMetadataJson: null,
        isEnabled: true,
        createdAt: "2026-07-03T00:00:00Z",
        updatedAt: "2026-07-03T00:00:00Z",
      },
    ],
    ...overrides,
  };
}

describe("ModelSourceEditDialog", () => {
  it.each([
    { provider: "openai", apiFallbackOnly: true },
    { provider: "openai", apiFallbackOnly: false },
    { provider: "anthropic", apiFallbackOnly: true },
    { provider: "anthropic", apiFallbackOnly: false },
  ] as const)("submits $provider fallback-only set to $apiFallbackOnly", async ({ provider, apiFallbackOnly }) => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const source = createModelSource({
      provider, authType: "api_key", billingMode: "api_metered", apiFallbackOnly: !apiFallbackOnly,
    });
    renderWithProviders(<ModelSourceEditDialog open busy={false} source={source} onOpenChange={vi.fn()} onSubmit={onSubmit} />);

    const fallback = screen.getByRole("switch", { name: "Use API only after subscriptions" });
    expect(fallback).toHaveAttribute("aria-checked", String(!apiFallbackOnly));
    expect(fallback).toHaveAccessibleDescription(/same supplier.*exact requested model/);
    await user.click(fallback);
    expect(fallback).toHaveAttribute("aria-checked", String(apiFallbackOnly));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit).toHaveBeenCalledWith(source.id, expect.objectContaining({ apiFallbackOnly }));
    expect(onSubmit.mock.calls[0][1].models).toBeUndefined();
  });

  it.each(["subscription", "unknown"] as const)("does not offer fallback-only for %s sources", (billingMode) => {
    renderWithProviders(<ModelSourceEditDialog open busy={false} source={createModelSource({ billingMode })} onOpenChange={vi.fn()} onSubmit={vi.fn()} />);
    expect(screen.queryByRole("switch", { name: "Use API only after subscriptions" })).not.toBeInTheDocument();
  });


  it("submits edited pricing and omits blank api key", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);

    renderWithProviders(
      <ModelSourceEditDialog
        open
        busy={false}
        source={createModelSource()}
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    const outputPrice = screen.getByDisplayValue("1.5");
    await user.clear(outputPrice);
    await user.type(outputPrice, "2.25");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });

    const [sourceId, payload] = onSubmit.mock.calls[0];
    expect(sourceId).toBe("src_1");
    expect("apiKey" in payload).toBe(false);
    expect(payload.models).toHaveLength(1);
    expect(payload.models[0]).toMatchObject({
      model: "Qwen/Qwen3.6-27B-FP8",
      inputPer1M: 0.5,
      outputPer1M: 2.25,
    });
    expect(payload.supportsAudioTranscriptions).toBe(false);
    expect(payload.supportsEmbeddings).toBe(false);
  });


  it("preserves disabled model rows during edits", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const source = createModelSource({
      models: [
        {
          ...createModelSource().models[0],
          model: "enabled-model",
          displayName: "enabled-model",
          isEnabled: true,
          contextWindow: 2048,
          supportsVision: true,
        },
        {
          ...createModelSource().models[0],
          id: 2,
          model: "disabled-model",
          displayName: "disabled-model",
          isEnabled: false,
          contextWindow: 4096,
          supportsVision: false,
        },
      ],
    });

    renderWithProviders(
      <ModelSourceEditDialog
        open
        busy={false}
        source={source}
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    const outputPrice = screen.getByDisplayValue("1.5");
    await user.clear(outputPrice);
    await user.type(outputPrice, "2.0");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });

    expect(onSubmit.mock.calls[0][1].models).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          model: "enabled-model",
          isEnabled: true,
          contextWindow: 2048,
          outputPer1M: 2,
          supportsVision: true,
        }),
        expect.objectContaining({
          model: "disabled-model",
          isEnabled: false,
          contextWindow: 4096,
          outputPer1M: 2,
          supportsVision: false,
        }),
      ]),
    );
  });

  it("omits model rows when only source-level fields changed", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const source = createModelSource({
      models: [
        {
          ...createModelSource().models[0],
          model: "m-one",
          displayName: "m-one",
          contextWindow: 2048,
          outputPer1M: 1.0,
        },
        {
          ...createModelSource().models[0],
          id: 2,
          model: "m-two",
          displayName: "m-two",
          contextWindow: 4096,
          outputPer1M: 2.0,
        },
      ],
    });

    renderWithProviders(
      <ModelSourceEditDialog
        open
        busy={false}
        source={source}
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    await user.clear(screen.getByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "vllm-local-updated");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });

    const payload = onSubmit.mock.calls[0][1];
    expect(payload.name).toBe("vllm-local-updated");
    expect(payload.models).toBeUndefined();
  });

  it("prefills and submits the audio per-minute rate", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const source = createModelSource();
    source.models[0].audioPerMinute = 0.3;

    renderWithProviders(
      <ModelSourceEditDialog
        open
        busy={false}
        source={source}
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    const audioPrice = screen.getByDisplayValue("0.3");
    await user.clear(audioPrice);
    await user.type(audioPrice, "0.45");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });

    expect(onSubmit.mock.calls[0][1].models[0].audioPerMinute).toBe(0.45);
  });

  it("toggles reasoning support via raw metadata while keeping other keys", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const source = createModelSource();
    source.models[0].rawMetadataJson = '{"custom_key": "kept"}';

    renderWithProviders(
      <ModelSourceEditDialog
        open
        busy={false}
        source={source}
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    expect(screen.getByRole("checkbox", { name: "Reasoning" })).not.toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: "Reasoning" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });

    const rawMetadata = JSON.parse(onSubmit.mock.calls[0][1].models[0].rawMetadataJson);
    expect(rawMetadata).toEqual({
      custom_key: "kept",
      supports_reasoning: true,
      supported_reasoning_levels: ["low", "medium", "high"],
      default_reasoning_level: "medium",
    });
  });

  it("prefills the reasoning toggle from raw metadata", () => {
    const source = createModelSource();
    source.models[0].rawMetadataJson = JSON.stringify({
      supports_reasoning: true,
      supported_reasoning_levels: [
        { effort: "none", description: "disable chain of thought" },
        "provider-specific",
        "ultra",
      ],
      default_reasoning_level: "provider-specific",
    });

    renderWithProviders(
      <ModelSourceEditDialog
        open
        busy={false}
        source={source}
        onOpenChange={vi.fn()}
        onSubmit={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(screen.getByRole("checkbox", { name: "Reasoning" })).toBeChecked();
    expect(screen.getByLabelText("Supported reasoning efforts")).toHaveValue(
      "none, provider-specific, ultra",
    );
    expect(screen.getByRole("combobox", { name: "Default reasoning effort" })).toHaveTextContent(
      "provider-specific",
    );
  });

  it("keeps arbitrary reasoning efforts and renormalizes a stale default", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const source = createModelSource();
    source.models[0].rawMetadataJson = JSON.stringify({
      supports_reasoning: true,
      supported_reasoning_levels: ["none", "provider-specific", "ultra"],
      default_reasoning_level: "provider-specific",
    });

    renderWithProviders(
      <ModelSourceEditDialog
        open
        busy={false}
        source={source}
        onOpenChange={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    const reasoningEfforts = screen.getByLabelText("Supported reasoning efforts");
    await user.clear(reasoningEfforts);
    await user.type(reasoningEfforts, "none, custom-tier");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });

    const rawMetadata = JSON.parse(onSubmit.mock.calls[0][1].models[0].rawMetadataJson);
    expect(rawMetadata).toEqual({
      supports_reasoning: true,
      supported_reasoning_levels: ["none", "custom-tier"],
      default_reasoning_level: "none",
    });
  });

  it("edits one API catalog model without copying its limits or prices to other models", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const unknown = {
      ...createModelSource().models[0], contextWindow: null, maxOutputTokens: null,
      supportsTools: false, inputPer1M: null, outputPer1M: null,
    };
    const source = createModelSource({
      provider: "openai", authType: "api_key", billingMode: "api_metered", baseUrl: "https://api.openai.com/v1",
      models: [
        { ...unknown, model: "openai/one", upstreamModel: "one" },
        { ...unknown, id: 2, model: "openai/two", upstreamModel: "two" },
        { ...unknown, id: 3, model: "openai/untouched", upstreamModel: "untouched",
          contextWindow: 8192, maxOutputTokens: 512, outputPer1M: 2, isEnabled: false },
      ],
    });
    renderWithProviders(<ModelSourceEditDialog open busy={false} source={source} onOpenChange={vi.fn()} onSubmit={onSubmit} />);

    expect(screen.getByLabelText("Base URL")).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "Chat completions" })).toBeDisabled();
    await user.clear(screen.getByRole("spinbutton", { name: "Priority" }));
    await user.type(screen.getByRole("spinbutton", { name: "Priority" }), "9");
    await user.click(screen.getByRole("switch", { name: "Use API only after subscriptions" }));
    await user.type(screen.getByLabelText("Context Window"), "32768");
    await user.type(screen.getByLabelText("Max Output Tokens"), "1024");
    await user.type(screen.getByLabelText("Input"), "0.75");
    await user.click(screen.getByRole("checkbox", { name: "Tools" }));

    await user.click(screen.getByRole("combobox", { name: "Edit catalog model" }));
    await user.click(screen.getByRole("option", { name: "openai/two" }));
    expect(screen.getByLabelText("Context Window")).toHaveValue("");
    expect(screen.getByLabelText("Input")).toHaveValue("");
    expect(screen.getByRole("checkbox", { name: "Tools" })).not.toBeChecked();
    expect(screen.getByRole("spinbutton", { name: "Priority" })).toHaveValue(9);
    expect(screen.getByRole("switch", { name: "Use API only after subscriptions" })).toBeChecked();

    await user.click(screen.getByRole("combobox", { name: "Edit catalog model" }));
    await user.click(screen.getByRole("option", { name: "openai/one" }));
    expect(screen.getByLabelText("Context Window")).toHaveValue("32768");
    expect(screen.getByLabelText("Max Output Tokens")).toHaveValue("1024");
    expect(screen.getByLabelText("Input")).toHaveValue("0.75");
    expect(screen.getByRole("checkbox", { name: "Tools" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Use API only after subscriptions" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const payload = onSubmit.mock.calls[0][1];
    expect(payload.priority).toBe(9);
    expect(payload.apiFallbackOnly).toBe(true);
    expect(payload.models).toMatchObject([
      { model: "openai/one", upstreamModel: "one", contextWindow: 32768, maxOutputTokens: 1024, inputPer1M: 0.75, supportsTools: true },
      { model: "openai/two", upstreamModel: "two", contextWindow: null, maxOutputTokens: null, inputPer1M: null, supportsTools: false },
      { model: "openai/untouched", upstreamModel: "untouched", contextWindow: 8192, maxOutputTokens: 512, outputPer1M: 2, isEnabled: false },
    ]);
  });
});
