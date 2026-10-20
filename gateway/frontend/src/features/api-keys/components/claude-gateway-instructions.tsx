import { useTranslation } from "react-i18next";

export function ClaudeGatewayInstructions() {
  const { t } = useTranslation();
  const endpoint = `${window.location.origin}/v1/messages`;
  const example = `curl '${endpoint}' \\\n  -H "x-api-key: $GATEWAY_API_KEY" \\\n  -H 'anthropic-version: 2023-06-01' \\\n  -H 'content-type: application/json' \\\n  -d '{"model":"<allowed-claude-model>","max_tokens":128,"messages":[{"role":"user","content":"Hello"}]}'`;
  return (
    <section className="space-y-2 rounded-md border p-3 text-xs" aria-label={t("apiKeyMonitoring.gatewayTitle")}>
      <h3 className="font-semibold">{t("apiKeyMonitoring.gatewayTitle")}</h3>
      <code className="block break-all">POST {endpoint}</code>
      <p className="text-muted-foreground">{t("apiKeyMonitoring.gatewayHelp")}</p>
      <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-muted p-2">{example}</pre>
    </section>
  );
}
