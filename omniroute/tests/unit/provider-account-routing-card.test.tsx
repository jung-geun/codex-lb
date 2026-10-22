import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProviderAccountRoutingCard from "../../src/app/(dashboard)/dashboard/settings/components/ProviderAccountRoutingCard";
import messages from "../../src/i18n/messages/en.json";

const fetchMock = vi.fn<typeof fetch>();

function mount() {
  return render(
    <NextIntlClientProvider locale="en" timeZone="UTC" messages={messages}>
      <ProviderAccountRoutingCard providerKey="codex" connectionCount={3} />
    </NextIntlClientProvider>
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("provider account routing settings", () => {
  it("does not invent the global strategy or save while the initial read is pending", async () => {
    let resolve!: (response: Response) => void;
    fetchMock.mockReturnValue(new Promise<Response>((r) => { resolve = r; }));
    mount();
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    expect(select.disabled).toBe(true);
    expect(select.options[0].textContent).not.toContain("round-robin");
    expect(screen.queryByRole("spinbutton")).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]?.method).toBeUndefined();

    await act(async () => {
      resolve(new Response(JSON.stringify({ fallbackStrategy: "p2c" })));
    });
    expect(select.disabled).toBe(false);
    expect(select.value).toBe("");
    expect(select.options[0].textContent).toContain("(p2c)");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([7, undefined])("shows the inherited global round-robin count (%s) without writing", async (count) => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({
      fallbackStrategy: "round-robin",
      stickyRoundRobinLimit: count,
    })));
    mount();
    const input = await screen.findByRole("spinbutton") as HTMLInputElement;
    expect(input.disabled).toBe(true);
    expect(input.value).toBe("");
    expect(input.placeholder).toBe(String(count ?? 3));
    const hint = document.getElementById(input.getAttribute("aria-describedby") || "");
    expect(hint?.textContent).toContain(String(count ?? 3));
    expect((screen.getByRole("combobox") as HTMLSelectElement).options[0].textContent)
      .toContain("(round-robin)");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("honors a stored provider batch even when the strategy itself inherits global round-robin", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({
      fallbackStrategy: "round-robin",
      stickyRoundRobinLimit: 7,
      providerStrategies: { codex: { stickyRoundRobinLimit: 12 } },
    })));
    mount();
    const input = await screen.findByRole("spinbutton") as HTMLInputElement;
    expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("");
    expect(input.value).toBe("12");
    expect(input.placeholder).toBe("7");
    expect(document.getElementById(input.getAttribute("aria-describedby") || "")?.textContent)
      .toContain("12");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not claim a default strategy when the settings read fails", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 503 }));
    mount();
    await screen.findByRole("alert");
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    expect(select.disabled).toBe(true);
    expect(select.options[0].textContent).not.toContain("(fill-first)");
    expect(screen.queryByRole("spinbutton")).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps the applied provider strategy and batch visible after an inherit write fails", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const saved = {
      settingsRevision: 5,
      fallbackStrategy: "priority",
      stickyRoundRobinLimit: 9,
      providerStrategies: { codex: { fallbackStrategy: "round-robin", stickyRoundRobinLimit: 4 } },
    };
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify(saved)))
      .mockResolvedValueOnce(new Response(JSON.stringify(saved)))
      .mockResolvedValueOnce(new Response("{}", { status: 500 }));
    const { container } = mount();
    const input = await screen.findByRole("spinbutton") as HTMLInputElement;
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "" } });
    await screen.findByRole("alert");
    expect(select.value).toBe("");
    const appliedSummary = container.querySelector('[aria-live="polite"] > p');
    expect(appliedSummary?.textContent).toContain("(round-robin)");
    expect(appliedSummary?.textContent).not.toContain("(priority)");
    expect(screen.getByRole("spinbutton")).toBe(input);
    expect(input.value).toBe("4");
    expect(document.getElementById(input.getAttribute("aria-describedby") || "")?.textContent)
      .toContain("4");
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("updates effective round-robin overrides, clears to inherit, and preserves other providers with CAS", async () => {
    let settings = {
      settingsRevision: 12,
      fallbackStrategy: "priority",
      stickyRoundRobinLimit: 9,
      providerStrategies: {
        codex: { fallbackStrategy: "round-robin", stickyRoundRobinLimit: 4 },
        claude: { fallbackStrategy: "p2c", stickyRoundRobinLimit: 2 },
      } as Record<string, { fallbackStrategy?: string; stickyRoundRobinLimit?: number }>,
    };
    const patches: Array<Record<string, unknown>> = [];
    fetchMock.mockImplementation(async (_url, init) => {
      if (init?.method === "PATCH") {
        const patch = JSON.parse(String(init.body));
        patches.push(patch);
        settings = { ...settings, providerStrategies: patch.providerStrategies, settingsRevision: 13 };
        return new Response("{}");
      }
      return new Response(JSON.stringify(settings));
    });
    mount();
    const input = await screen.findByRole("spinbutton") as HTMLInputElement;
    expect(input.value).toBe("4");
    expect(input.placeholder).toBe("9");
    expect(input.disabled).toBe(false);

    fireEvent.change(input, { target: { value: "6" } });
    fireEvent.blur(input);
    await waitFor(() => expect(input.disabled).toBe(false));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({
      expectedRevision: 12,
      providerStrategies: {
        codex: { fallbackStrategy: "round-robin", stickyRoundRobinLimit: 6 },
        claude: { fallbackStrategy: "p2c", stickyRoundRobinLimit: 2 },
      },
    });
    expect(input.value).toBe("6");
    expect(document.getElementById(input.getAttribute("aria-describedby") || "")?.textContent)
      .toContain("6");

    fireEvent.change(input, { target: { value: "" } });
    fireEvent.blur(input);
    await waitFor(() => expect(patches).toHaveLength(2));
    await waitFor(() => expect(input.disabled).toBe(false));
    expect(patches[1]).toEqual({
      expectedRevision: 13,
      providerStrategies: {
        codex: { fallbackStrategy: "round-robin" },
        claude: { fallbackStrategy: "p2c", stickyRoundRobinLimit: 2 },
      },
    });
    expect(input.value).toBe("");
    expect(input.placeholder).toBe("9");
    expect(document.getElementById(input.getAttribute("aria-describedby") || "")?.textContent)
      .toContain("9");
  });

  it("refreshes the override and global batch after a conflict without retrying the write", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const original = {
      settingsRevision: 3,
      fallbackStrategy: "priority",
      stickyRoundRobinLimit: 5,
      providerStrategies: { codex: { fallbackStrategy: "random" } },
    };
    const refreshed = {
      settingsRevision: 4,
      fallbackStrategy: "round-robin",
      stickyRoundRobinLimit: 11,
      providerStrategies: { claude: { fallbackStrategy: "least-used" } },
    };
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify(original)))
      .mockResolvedValueOnce(new Response(JSON.stringify(original)))
      .mockResolvedValueOnce(new Response("{}", { status: 409 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(refreshed)));
    mount();
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe("random"));
    fireEvent.change(select, { target: { value: "round-robin" } });
    await screen.findByRole("alert");
    expect(select.value).toBe("");
    expect(select.options[0].textContent).toContain("(round-robin)");
    const input = screen.getByRole("spinbutton") as HTMLInputElement;
    expect(input.disabled).toBe(true);
    expect(input.placeholder).toBe("11");
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });
});
