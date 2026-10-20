import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/utils";

import { ModelSourceCreateDialog } from "./model-source-create-dialog";

const fallbackName = "Use API only after subscriptions";

describe("ModelSourceCreateDialog", () => {
  it.each([
    { provider: "openai", apiFallbackOnly: true },
    { provider: "openai", apiFallbackOnly: false },
    { provider: "anthropic", apiFallbackOnly: true },
    { provider: "anthropic", apiFallbackOnly: false },
  ] as const)("registers $provider with fallback-only set to $apiFallbackOnly", async ({ provider, apiFallbackOnly }) => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(<ModelSourceCreateDialog open busy={false} initialProvider={provider} onOpenChange={vi.fn()} onSubmit={onSubmit} />);

    const fallback = await screen.findByRole("switch", { name: fallbackName });
    expect(fallback).toBeChecked();
    expect(fallback).toHaveAccessibleDescription(/same supplier.*exact requested model/);
    expect(screen.queryByRole("textbox", { name: "Base URL" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Provider / key type" })).not.toBeInTheDocument();
    await user.click(fallback);
    if (apiFallbackOnly) await user.click(fallback);
    expect(fallback).toHaveAttribute("aria-checked", String(apiFallbackOnly));
    await user.type(screen.getByLabelText("Upstream API key"), "synthetic-provider-key");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      provider, authType: "api_key", billingMode: "api_metered", apiFallbackOnly, models: [],
    }));
  });

  it("does not offer fallback-only for custom or subscription sources", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ModelSourceCreateDialog open busy={false} onOpenChange={vi.fn()} onSubmit={vi.fn()} />);
    expect(screen.queryByRole("switch", { name: fallbackName })).not.toBeInTheDocument();

    const presets = screen.getByRole("combobox", { name: "Provider / key type" });
    await waitFor(() => expect(presets).toBeEnabled());
    await user.click(presets);
    await user.click(screen.getByRole("option", { name: "Claude subscription" }));
    expect(screen.queryByRole("switch", { name: fallbackName })).not.toBeInTheDocument();
  });

  it("resets fallback-only when changing provider presets", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(<ModelSourceCreateDialog open busy={false} onOpenChange={vi.fn()} onSubmit={onSubmit} />);
    const presets = screen.getByRole("combobox", { name: "Provider / key type" });
    await waitFor(() => expect(presets).toBeEnabled());
    await user.click(presets);
    await user.click(screen.getByRole("option", { name: "OpenAI / ChatGPT API" }));
    await user.click(screen.getByRole("switch", { name: fallbackName }));

    await user.click(presets);
    await user.click(screen.getByRole("option", { name: "Claude subscription" }));
    expect(screen.queryByRole("switch", { name: fallbackName })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ provider: "claude", apiFallbackOnly: true }));
  });
});
