import { useEffect, useRef, useState } from "react";

import { ModelSourceCreateDialog, type ModelSourceCreateDialogProps } from "@/features/model-sources/components/model-source-create-dialog";
import { ModelSourceConnectDialog } from "@/features/model-sources/components/model-source-connect-dialog";
import { useModelSources } from "@/features/model-sources/hooks/use-model-sources";
import type { ModelSource } from "@/features/model-sources/schemas";

export type SourceConnectionFlowProps = {
  createOpen: boolean;
  onCreateOpenChange: (open: boolean) => void;
  initialProvider?: ModelSourceCreateDialogProps["initialProvider"];
  onCreated?: (source: ModelSource) => void;
};

export function SourceConnectionFlow({
  createOpen,
  onCreateOpenChange,
  initialProvider,
  onCreated,
}: SourceConnectionFlowProps) {
  const { createMutation } = useModelSources();
  const [pendingConnection, setPendingConnection] = useState<ModelSource | null>(null);
  const [connectSource, setConnectSource] = useState<ModelSource | null>(null);
  const active = useRef(true);

  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);

  useEffect(() => {
    if (createOpen || !pendingConnection) return;
    // Wait until the create modal has closed before mounting the connection modal.
    const frame = requestAnimationFrame(() => {
      setConnectSource(pendingConnection);
      setPendingConnection(null);
    });
    return () => cancelAnimationFrame(frame);
  }, [createOpen, pendingConnection]);

  return (
    <>
      <ModelSourceCreateDialog
        open={createOpen}
        busy={createMutation.isPending}
        initialProvider={initialProvider}
        onOpenChange={onCreateOpenChange}
        onSubmit={async (payload) => {
          try {
            const source = await createMutation.mutateAsync(payload);
            if (!active.current) return;
            onCreated?.(source);
            if (source.authType === "claude_oauth") setPendingConnection(source);
          } finally {
            // reset() detaches the observer but leaves cached mutation variables.
            payload.apiKey = undefined;
            createMutation.reset();
          }
        }}
      />
      {connectSource ? (
        <ModelSourceConnectDialog
          key={connectSource.id}
          source={connectSource}
          onClose={() => setConnectSource(null)}
        />
      ) : null}
    </>
  );
}
