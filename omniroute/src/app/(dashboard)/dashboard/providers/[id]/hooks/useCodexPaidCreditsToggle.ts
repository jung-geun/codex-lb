"use client";

/**
 * useCodexPaidCreditsToggle — extracted from useProviderConnections (file-size
 * ratchet: useProviderConnections.ts is frozen; a new full handler pushed it
 * over the cap) to keep the god-file from growing, mirroring the
 * useReorderByAvailability precedent in this same directory.
 *
 * Owns the single-connection toggle for the opt-in "allow paid Codex
 * credits after subscription quota exhaustion" setting.
 */

import { useRef } from "react";
import type { ConnectionRowConnection } from "../components/ConnectionRow";
import { providerText, type ProviderMessageTranslator } from "../providerCredentialText";

/** Minimal surface of the notification store this hook needs. */
interface CodexPaidCreditsToggleNotifier {
  success: (message: string) => void;
  error: (message: string) => void;
}

export interface UseCodexPaidCreditsToggleParams {
  connections: ConnectionRowConnection[];
  setConnections: (
    updater:
      ConnectionRowConnection[] | ((prev: ConnectionRowConnection[]) => ConnectionRowConnection[])
  ) => void;
  notify: CodexPaidCreditsToggleNotifier;
  t: ProviderMessageTranslator;
}

export interface UseCodexPaidCreditsToggleReturn {
  handleToggleCodexPaidCredits: (connectionId: string, enabled: boolean) => Promise<void>;
}

export function useCodexPaidCreditsToggle({
  connections,
  setConnections,
  notify,
  t,
}: UseCodexPaidCreditsToggleParams): UseCodexPaidCreditsToggleReturn {
  const pendingIds = useRef(new Set<string>());
  const handleToggleCodexPaidCredits = async (connectionId: string, enabled: boolean) => {
    if (pendingIds.current.has(connectionId)) return;
    pendingIds.current.add(connectionId);
    try {
      const target = connections.find((connection) => connection.id === connectionId);
      if (!target || target.provider !== "codex") return;

      const res = await fetch(`/api/providers/${connectionId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          providerSpecificData: { allowPaidCredits: enabled },
        }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        notify.error(
          data.error ||
            providerText(
              t,
              "failedUpdateCodexPaidCredits",
              "Failed to update Codex paid-credit policy"
            )
        );
        return;
      }

      setConnections((prev) =>
        prev.map((connection) =>
          connection.id === connectionId
            ? {
                ...connection,
                providerSpecificData: {
                  ...(connection.providerSpecificData || {}),
                  allowPaidCredits: enabled,
                },
              }
            : connection
        )
      );
      notify.success(
        enabled
          ? providerText(
              t,
              "codexPaidCreditsEnabled",
              "Automatic credit use after subscription exhaustion enabled"
            )
          : providerText(t, "codexPaidCreditsDisabled", "Automatic credit use disabled")
      );
    } catch (error) {
      console.error("Error toggling Codex paid-credit policy:", error);
      notify.error(
        providerText(t, "failedUpdateCodexPaidCredits", "Failed to update Codex paid-credit policy")
      );
    } finally {
      pendingIds.current.delete(connectionId);
    }
  };

  return { handleToggleCodexPaidCredits };
}
