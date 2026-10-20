import { useId } from "react";
import { useTranslation } from "react-i18next";

import { CopyButton } from "@/components/copy-button";
import { cn } from "@/lib/utils";

export type ConnectionGuidesProps = {
  className?: string;
};

export function ConnectionGuides({ className }: ConnectionGuidesProps) {
  const { t } = useTranslation();
  const headingId = useId();
  const origin = window.location.origin;
  const catalogCommand = `curl --fail --silent --show-error \\\n  --header "Authorization: Bearer \${CODEX_LB_API_KEY:?}" \\\n  '${origin}/v1/models'`;
  const claudeCommand = `ANTHROPIC_BASE_URL='${origin}' \\\nANTHROPIC_API_KEY= \\\nCLAUDE_CODE_OAUTH_TOKEN= \\\nANTHROPIC_AUTH_TOKEN="\${CODEX_LB_API_KEY:?}" \\\nclaude --model "\${CLAUDE_MODEL:?}"`;
  const ompCommand = `uv run scripts/connect-omp.py \\\n  --base-url '${origin}' \\\n  --key-env CODEX_LB_API_KEY`;

  return (
    <section className={cn("min-w-0 space-y-4", className)} aria-labelledby={headingId}>
      <div className="space-y-1">
        <h3 id={headingId} className="text-sm font-semibold">{t("connectionGuides.title")}</h3>
        <p className="text-sm text-muted-foreground">{t("connectionGuides.description")}</p>
        <p className="text-xs text-muted-foreground">{t("connectionGuides.keySetup")}</p>
      </div>

      <div className="min-w-0 space-y-2 rounded-lg border p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h4 className="text-sm font-medium">{t("connectionGuides.catalogTitle")}</h4>
          <CopyButton value={catalogCommand} label={t("connectionGuides.copyCatalog")} iconOnly />
        </div>
        <p className="text-xs text-muted-foreground">{t("connectionGuides.catalogDescription")}</p>
        <pre className="max-w-full overflow-x-auto rounded-md bg-muted p-3 text-xs"><code>{catalogCommand}</code></pre>
      </div>

      <div className="grid min-w-0 gap-3 xl:grid-cols-2">
        <div className="min-w-0 space-y-2 rounded-lg border p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h4 className="text-sm font-medium">{t("connectionGuides.claudeTitle")}</h4>
            <CopyButton value={claudeCommand} label={t("connectionGuides.copyClaude")} iconOnly />
          </div>
          <p className="text-xs text-muted-foreground">{t("connectionGuides.claudeDescription")}</p>
          <pre className="max-w-full overflow-x-auto rounded-md bg-muted p-3 text-xs"><code>{claudeCommand}</code></pre>
          <p className="text-xs text-muted-foreground">{t("connectionGuides.claudeEndpoint")}</p>
        </div>
        <div className="min-w-0 space-y-2 rounded-lg border p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h4 className="text-sm font-medium">{t("connectionGuides.ompTitle")}</h4>
            {window.location.protocol === "https:" ? (
              <CopyButton value={ompCommand} label={t("connectionGuides.copyOmp")} iconOnly />
            ) : null}
          </div>
          <p className="text-xs text-muted-foreground">{t("connectionGuides.ompDescription")}</p>
          {window.location.protocol === "https:" ? (
            <pre className="max-w-full overflow-x-auto rounded-md bg-muted p-3 text-xs"><code>{ompCommand}</code></pre>
          ) : (
            <p className="text-xs text-muted-foreground">{t("connectionGuides.ompHttpsRequired")}</p>
          )}
          <p className="text-xs text-muted-foreground">{t("connectionGuides.ompProfile")}</p>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">{t("connectionGuides.billingNote")}</p>
    </section>
  );
}
