"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import Card from "@/shared/components/Card";
import Toggle from "@/shared/components/Toggle";
import Input from "@/shared/components/Input";
import Button from "@/shared/components/Button";

type Policy = { enabled: boolean; subscriptionOnlyKeyIds: string[] };
type KeyLabel = { id: string; name: string };
type MessageKey = "nativeRoutingLoadError" | "nativeRoutingSaved" | "nativeRoutingSaveError";

export default function NativeSubscriptionRoutingCard() {
  const t = useTranslations("settings");
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [keys, setKeys] = useState<KeyLabel[]>([]);
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<MessageKey | "">("");
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
        if (!controller.signal.aborted) { setFailed(true); setMessage("nativeRoutingLoadError"); }
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
      setMessage("nativeRoutingSaved");
      setFailed(false);
    } catch {
      setFailed(true);
      setMessage("nativeRoutingSaveError");
    } finally { setPassword(""); setSaving(false); }
  }

  return (
    <Card title={t("nativeRoutingTitle")} subtitle={t("nativeRoutingSubtitle")}>
      <div className="space-y-4">
        <Toggle label={t("nativeRoutingAllowPaidFallback")} checked={policy?.enabled ?? false}
          disabled={!policy || saving} onChange={(enabled) => setPolicy((value) => value && ({ ...value, enabled }))} />
        <p className="text-sm text-text-muted">{t("nativeRoutingSafety")}</p>
        <fieldset disabled={!policy || saving} className="space-y-3">
          <legend className="mb-2 text-sm font-medium text-text-main">{t("nativeRoutingSubscriptionOnlyKeys")}</legend>
          <p className="text-sm text-text-muted">{t("nativeRoutingSubscriptionOnlyScope")}</p>
          {keys.map((key) => <Toggle key={key.id} label={key.name || key.id}
            checked={policy?.subscriptionOnlyKeyIds.includes(key.id) ?? false}
            onChange={(checked) => setPolicy((value) => value && ({ ...value, subscriptionOnlyKeyIds: checked
              ? [...new Set([...value.subscriptionOnlyKeyIds, key.id])]
              : value.subscriptionOnlyKeyIds.filter((id) => id !== key.id) }))} />)}
        </fieldset>
        <Input type="password" autoComplete="current-password" label={t("nativeRoutingCurrentPassword")}
          value={password} disabled={!policy || saving} onChange={(event) => setPassword(event.target.value)} />
        <Button disabled={!policy || !password} loading={saving} onClick={() => void save()}>{t("nativeRoutingSave")}</Button>
        {!policy && failed && <Button variant="secondary" onClick={() => setAttempt((value) => value + 1)}>{t("nativeRoutingRetry")}</Button>}
        {message && <p role={failed ? "alert" : "status"} className="text-sm text-text-muted">{t(message)}</p>}
      </div>
    </Card>
  );
}
