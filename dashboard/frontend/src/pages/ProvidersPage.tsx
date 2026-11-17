import { useCallback, useEffect, useState } from "react";

import { ApiRequestError, getProviders, setLunaEnabled } from "../api";
import type { ProviderSnapshot } from "../api";

export function ProvidersPage({ csrfToken, onSessionExpired }: {
  csrfToken: string;
  onSessionExpired: () => void;
}) {
  const [snapshot, setSnapshot] = useState<ProviderSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [cloudAcknowledged, setCloudAcknowledged] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setSnapshot(await getProviders());
      setError(null);
    } catch (cause) {
      if (cause instanceof ApiRequestError && cause.status === 401) onSessionExpired();
      setError(cause instanceof ApiRequestError && cause.status === 503
        ? "Inference gateway is not configured or is offline."
        : "Provider status could not be loaded.");
    }
  }, [onSessionExpired]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 15_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const luna = snapshot?.providers.find((provider) => provider.id === "codex_cli");
  const local = snapshot?.providers.find((provider) => provider.id === "local_qwen");
  const toggle = async () => {
    if (!luna) return;
    setBusy(true);
    setError(null);
    try {
      await setLunaEnabled(!luna.enabled, csrfToken, cloudAcknowledged);
      setCloudAcknowledged(false);
      await refresh();
    } catch (cause) {
      if (cause instanceof ApiRequestError && cause.status === 401) onSessionExpired();
      setError("Provider setting could not be saved.");
    } finally {
      setBusy(false);
    }
  };

  const state = (provider?: ProviderSnapshot["providers"][number]) =>
    !provider ? "Unknown" : !provider.configured ? "Not configured" : !provider.enabled ? "Disabled" : provider.ready ? "Ready" : "Unavailable";
  const averageLatency = (provider?: ProviderSnapshot["providers"][number]) =>
    provider && provider.latency_samples > 0
      ? `${Math.round(provider.total_latency_ms / provider.latency_samples)} ms`
      : "—";
  const healthLatency = (provider?: ProviderSnapshot["providers"][number]) =>
    provider?.health_latency_ms == null ? "—" : `${provider.health_latency_ms} ms`;
  const lastHealthCheck = (provider?: ProviderSnapshot["providers"][number]) =>
    provider?.last_health_check ? new Date(provider.last_health_check).toLocaleTimeString() : "—";

  return <div className="page-content providers-content">
    <section className="intro-panel">
      <span className="eyebrow">INFERENCE ROUTING</span>
      <h2>Providers</h2>
      <p>Choose which configured inference providers the gateway can offer to API clients.</p>
    </section>
    {error && <p className="settings-message settings-error" role="alert">{error}</p>}
    <section className="panel settings-panel provider-card">
      <div className="panel-heading"><div><span className="eyebrow">LOCAL PROVIDER</span><h2>Local Qwen</h2></div><span className="readonly-tag">{state(local)}</span></div>
      <p>Model alias <code>ddu-narrator</code>. Requests stay on the local inference network.</p>
      <dl className="provider-facts"><dt>Configured</dt><dd>{local?.configured ? "Yes" : "Unknown"}</dd><dt>Enabled</dt><dd>{local?.enabled ? "Yes" : "No"}</dd><dt>Ready</dt><dd>{local?.ready ? "Yes" : "No"}</dd><dt>Requests</dt><dd>{local?.requests ?? "—"}</dd><dt>Average latency</dt><dd>{averageLatency(local)}</dd><dt>Health-check latency</dt><dd>{healthLatency(local)}</dd><dt>Last health check</dt><dd>{lastHealthCheck(local)}</dd><dt>Latest error</dt><dd>{local?.error_class ?? "None"}</dd></dl>
    </section>
    <section className="panel settings-panel provider-card">
      <div className="panel-heading"><div><span className="eyebrow">CLOUD PROVIDER</span><h2>GPT-6 Luna High</h2></div><span className="readonly-tag">{state(luna)}</span></div>
      <p>Model alias <code>gpt-6-luna</code>. Enabling sends prompts and supplied conversation context to OpenAI through the signed-in Codex CLI. Do not include information you do not want sent to the cloud.</p>
      <dl className="provider-facts"><dt>Configured</dt><dd>{luna?.configured ? "Yes" : "No"}</dd><dt>Enabled</dt><dd>{luna?.enabled ? "Yes" : "No"}</dd><dt>Ready</dt><dd>{luna?.ready ? "Yes" : "No"}</dd><dt>Requests</dt><dd>{luna?.requests ?? "—"}</dd><dt>Failures</dt><dd>{luna?.failures ?? "—"}</dd><dt>Average latency</dt><dd>{averageLatency(luna)}</dd><dt>Health-check latency</dt><dd>{healthLatency(luna)}</dd><dt>Last health check</dt><dd>{lastHealthCheck(luna)}</dd><dt>Latest error</dt><dd>{luna?.error_class ?? "None"}</dd></dl>
      {!luna?.enabled && <label className="provider-consent"><input type="checkbox" checked={cloudAcknowledged} onChange={(event) => setCloudAcknowledged(event.target.checked)} /> I understand prompts and context may be sent to OpenAI.</label>}
      <button className="primary-button" disabled={busy || !luna?.configured || (!luna.enabled && (!luna.ready || !cloudAcknowledged))} onClick={() => void toggle()} type="button">
        {busy ? "Saving…" : luna?.enabled ? "Disable GPT-6 Luna" : "Enable GPT-6 Luna"}
      </button>
    </section>
  </div>;
}
