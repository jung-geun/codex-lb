import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HttpResponse, http } from "msw";

import { renderWithProviders } from "@/test/utils";
import { createApiKey } from "@/test/mocks/factories";
import { ApiDetail } from "@/features/apis/components/api-detail";
import type { SourceModelAccess } from "@/features/api-keys/schemas";
import { SourceAccessPanel } from "./source-access-panel";
import { effectiveSourceAvailability, type SourceAccessPolicy } from "@/features/api-keys/source-access";
import { ClaudeGatewayInstructions } from "./claude-gateway-instructions";
import { ModelMultiSelect } from "./model-multi-select";
import { ApiKeyCreateDialog } from "./api-key-create-dialog";
import { createModelSource, ADMIN_PERMISSIONS } from "@/test/mocks/factories";
import { server } from "@/test/mocks/server";
import { useAuthStore } from "@/features/auth/hooks/use-auth";

const mocks = vi.hoisted(() => ({ useSourceAccess: vi.fn() }));
vi.mock("@/features/api-keys/hooks/use-source-access", () => mocks);

vi.mock("@/features/api-keys/hooks/use-models", () => ({ useModels: () => ({ data: [], isLoading: false }) }));
const entry: SourceModelAccess = {
  sourceId: "claude-source", sourceName: "Claude subscription", provider: "claude", billingMode: "subscription",
  model: "claude-configured", displayName: "Configured Claude", availability: "quota_exhausted", quotaWindows: [],
};
const policy: SourceAccessPolicy = {
  assignedSourceIds: ["claude-source"], sourceAssignmentScopeEnabled: true,
  allowedModels: ["claude-configured"], enforcedModel: "claude-configured", billingPolicy: "subscription_only",
};

beforeEach(() => {
  mocks.useSourceAccess.mockReturnValue({ data: { models: [entry] }, isPending: false, isError: false });
  useAuthStore.setState({ initialized: true, permissions: ADMIN_PERMISSIONS, canWrite: true });
});

describe("Claude key management access", () => {
  it("keeps configured exhausted models visible without claiming they are callable", () => {
    renderWithProviders(<SourceAccessPanel policy={policy} />);
    expect(screen.getByText("claude-configured")).toBeInTheDocument();
    expect(screen.getByText(/Quota exhausted — selectable/)).toBeInTheDocument();
    expect(screen.getByText(/Subscription quota exhaustion never enables metered fallback/)).toBeInTheDocument();
  });


  it("allows selecting configured exhausted Claude models without a callable catalog entry", async () => {
    const onChange = vi.fn();
    renderWithProviders(<ModelMultiSelect value={[]} onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: "All models" }));
    await userEvent.click(screen.getByRole("menuitemcheckbox", { name: /claude-configured/ }));
    expect(onChange).toHaveBeenCalledWith(["claude-configured"]);
    expect(screen.getByText(/Quota exhausted — selectable/)).toBeInTheDocument();
  });

  it("creates a Claude-scoped key with allowed and enforced models without changing Codex scope", async () => {
    const source = createModelSource({ id: entry.sourceId, name: entry.sourceName, provider: "claude", billingMode: "subscription" });
    source.models = source.models.map((model) => ({ ...model, sourceId: entry.sourceId, model: entry.model }));
    server.use(http.get("/api/model-sources/", () => HttpResponse.json({ sources: [source] })));
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(<ApiKeyCreateDialog open busy={false} onOpenChange={vi.fn()} onSubmit={onSubmit} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Name"), "Claude gateway key");
    await user.click(await screen.findByRole("button", { name: "All model sources" }));
    await user.click(screen.getByRole("menuitemcheckbox", { name: /Claude subscription/ }));
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "All models" }));
    await user.click(screen.getByRole("menuitemcheckbox", { name: /claude-configured/ }));
    await user.keyboard("{Escape}");
    await user.type(screen.getByLabelText("Enforced Model"), entry.model);
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ assignedSourceIds: [entry.sourceId], allowedModels: [entry.model], enforcedModel: entry.model })));
    expect(onSubmit.mock.calls[0][0]).not.toHaveProperty("assignedAccountIds");
    expect(onSubmit.mock.calls[0][0].applyToCodexModel).toBe(false);
  });
  it.each([
    { assignedSourceIds: [] },
    { allowedModels: ["other"] },
    { enforcedModel: "other" },
  ])("intersects source, allowed models and enforced model: %j", (change) => {
    expect(effectiveSourceAvailability({ ...entry, availability: "available" }, { ...policy, ...change })).toBe("restricted");
  });

  it("preserves unrestricted empty model convention but not empty source scopes", () => {
    expect(effectiveSourceAvailability(entry, { ...policy, allowedModels: [] })).toBe("quota_exhausted");
    expect(effectiveSourceAvailability(entry, { ...policy, assignedSourceIds: [] })).toBe("restricted");
    expect(effectiveSourceAvailability({ ...entry, billingMode: "api_metered" }, policy)).toBe("restricted");
  });

  it("shows native gateway usage with a gateway-key placeholder, not source credentials", () => {
    renderWithProviders(<ClaudeGatewayInstructions />);
    expect(screen.getByText(`POST ${window.location.origin}/v1/messages`)).toBeInTheDocument();
    expect(screen.getByText(/x-api-key: \$GATEWAY_API_KEY/)).toBeInTheDocument();
    expect(screen.getByText(/Existing full keys cannot be retrieved/)).toBeInTheDocument();
  });

  it("read-only API detail exposes access state without mutation controls", () => {
    renderWithProviders(<ApiDetail apiKey={createApiKey()} busy={false} readOnly onEdit={vi.fn()} onDelete={vi.fn()} onRegenerate={vi.fn()} onToggleActive={vi.fn()} />);
    expect(screen.getByRole("region", { name: "Configured source models and effective access" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Actions" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disable" })).not.toBeInTheDocument();
  });
});
