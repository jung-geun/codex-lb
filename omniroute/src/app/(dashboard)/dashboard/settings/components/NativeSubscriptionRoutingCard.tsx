"use client";

import { useEffect, useState } from "react";
import Card from "@/shared/components/Card";
import Toggle from "@/shared/components/Toggle";
import Input from "@/shared/components/Input";
import Button from "@/shared/components/Button";

type Policy = { enabled: boolean; subscriptionOnlyKeyIds: string[] };
type KeyLabel = { id: string; name: string };

export default function NativeSubscriptionRoutingCard() {
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [keys, setKeys] = useState<KeyLabel[]>([]);
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const responses = await Promise.all([
          fetch("/api/settings", { cache: "no-store", signal: controller.signal }),
          fetch("/api/keys", { cache: "no-store", signal: controller.signal }),
        ]);
        if (responses.some((response) => !response.ok)) throw new Error("load");
        const [settings, keyData] = await Promise.all(responses.map((response) => response.json()));
        if (controller.signal.aborted) return;
        setPolicy(settings.nativeSubscriptionRouting ?? { enabled: false, subscriptionOnlyKeyIds: [] });
        setKeys(keyData.keys ?? []);
        setFailed(false);
        setMessage("");
      } catch {
        if (!controller.signal.aborted) { setFailed(true); setMessage("Could not load routing policy."); }
      }
    })();
    return () => controller.abort();
  }, [attempt]);

  async function save() {
    if (!policy || saving || !password) return;
    setSaving(true);
    setMessage("");
    try {
      const response = await fetch("/api/settings", {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nativeSubscriptionRouting: policy, currentPassword: password }),
      });
      if (!response.ok) throw new Error("save");
      setPolicy((await response.json()).nativeSubscriptionRouting);
      setMessage("Routing policy saved.");
      setFailed(false);
    } catch {
      setFailed(true);
      setMessage("Could not save policy. Check your operator session and current password.");
    } finally { setPassword(""); setSaving(false); }
  }

  return (
    <Card title="Codex / Claude subscription-first" subtitle="Optional, exact-model paid API fallback. API-equivalent estimates are not subscription charges.">
      <div className="space-y-4">
        <Toggle label="Allow paid API only after subscription refusal" checked={policy?.enabled ?? false}
          disabled={!policy || saving} onChange={(enabled) => setPolicy((value) => value && ({ ...value, enabled }))} />
        <p className="text-sm text-text-muted">Only OpenAI ↔ Codex and Anthropic ↔ Claude with the exact observed model and permitted connection. Accepted, ambiguous and continuation requests never switch to paid API. Off by default.</p>
        <fieldset disabled={!policy || saving} className="space-y-3">
          <legend className="mb-2 text-sm font-medium text-text-main">Subscription-only API keys</legend>
          <p className="text-sm text-text-muted">Selected keys can use only Codex / Claude subscriptions, including when paid fallback is enabled. Other provider routes are denied.</p>
          {keys.map((key) => <Toggle key={key.id} label={key.name || key.id}
            checked={policy?.subscriptionOnlyKeyIds.includes(key.id) ?? false}
            onChange={(checked) => setPolicy((value) => value && ({ ...value, subscriptionOnlyKeyIds: checked
              ? [...new Set([...value.subscriptionOnlyKeyIds, key.id])]
              : value.subscriptionOnlyKeyIds.filter((id) => id !== key.id) }))} />)}
        </fieldset>
        <Input type="password" autoComplete="current-password" label="Current administrator password"
          value={password} disabled={!policy || saving} onChange={(event) => setPassword(event.target.value)} />
        <Button disabled={!policy || !password} loading={saving} onClick={() => void save()}>Save routing policy</Button>
        {!policy && failed && <Button variant="secondary" onClick={() => setAttempt((value) => value + 1)}>Retry</Button>}
        {message && <p role={failed ? "alert" : "status"} className="text-sm text-text-muted">{message}</p>}
      </div>
    </Card>
  );
}
