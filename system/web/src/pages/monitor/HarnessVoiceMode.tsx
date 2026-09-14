import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { HarnessQuestion } from "./HarnessQuestion";
import { harnessRequest } from "./harness-api";

interface VoiceMode {
  enabled: boolean;
  generation: number;
  machineId: string;
  agentId: string;
  pending?: { idempotencyKey: string; agentId: string; runId: string };
  error?: string;
}
interface Agent { agentId: string; name?: string }
const control: CSSProperties = {
  padding: "8px 12px", borderRadius: 4, border: "1px solid var(--lm-border)",
  background: "var(--lm-surface)", color: "var(--lm-text)", fontSize: 12,
};

export function HarnessVoiceMode({ connected }: { connected: boolean }) {
  const [mode, setMode] = useState<VoiceMode | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentsLoaded, setAgentsLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);

  useEffect(() => {
    if (busy) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const result = await harnessRequest("/voice-mode", { signal: controller.signal, cache: "no-store" });
        if (disposed) return;
        setMode(result as VoiceMode);
        setPollError(null);
      } catch (cause) {
        if (!disposed) setPollError(cause instanceof Error ? cause.message : "Could not read voice mode.");
      } finally {
        if (!disposed) timer = setTimeout(() => { void poll(); }, 2000);
      }
    };
    void poll();
    return () => { disposed = true; controller.abort(); if (timer) clearTimeout(timer); };
  }, [busy, refresh]);

  const mutate = async (path: string, body: object, method = "POST") => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await harnessRequest(path, { method, body: JSON.stringify(body) });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not update Harness voice mode.");
    } finally {
      // Refresh actual state after every result, including ambiguous network failures.
      setRefresh(value => value + 1);
      setBusy(false);
    }
  };

  const refreshAgents = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await harnessRequest("/agents", { cache: "no-store" });
      if (!Array.isArray(result?.agents)) throw new Error("Harness returned an invalid agent list.");
      setAgents(result.agents.filter((agent: Agent) => typeof agent.agentId === "string" && agent.agentId));
      setAgentsLoaded(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not list Harness agents.");
    } finally { setBusy(false); }
  };

  const unavailable = busy || !mode || Boolean(pollError);
  return <section aria-label="Harness-only voice" style={{ borderTop: "1px solid var(--lm-border)", marginTop: 14, paddingTop: 14, display: "flex", flexDirection: "column", gap: 10, fontSize: 12 }}>
    <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <input type="checkbox" role="switch" checked={mode?.enabled ?? false}
        disabled={unavailable || (!mode?.enabled && (!connected || !mode?.agentId || Boolean(mode?.pending)))}
        onChange={event => { void mutate("/voice-mode", { enabled: event.target.checked, agentId: mode?.agentId }, "PUT"); }} />
      <strong>Harness-only voice</strong>
    </label>
    <p style={{ margin: 0, lineHeight: 1.5, color: "var(--lm-text-dim)" }}>
      Send your spoken requests directly to the selected Harness agent. Replies play on the device as usual.
      This mode turns off when the device service restarts. Text chat keeps its normal behavior.
    </p>
    <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      Voice agent
      <select style={control} value={mode?.agentId ?? ""} disabled={unavailable || !connected || Boolean(mode?.pending)}
        onChange={event => { void mutate("/voice-mode", { enabled: mode?.enabled ?? false, agentId: event.target.value }, "PUT"); }}>
        <option value="" disabled>Select an agent</option>
        {mode?.agentId && !agents.some(agent => agent.agentId === mode.agentId) && <option value={mode.agentId}>{mode.agentId} (selected)</option>}
        {agents.map(agent => <option key={agent.agentId} value={agent.agentId}>{agent.name || agent.agentId}{agent.name ? ` · ${agent.agentId}` : ""}</option>)}
      </select>
    </label>
    <button type="button" style={control} disabled={busy || !connected} onClick={() => { void refreshAgents(); }}>Refresh agents</button>
    {agentsLoaded && agents.length === 0 && <span>No agents are available. Start an agent in Harness and refresh.</span>}
    {!connected && <p role="status" style={{ margin: 0, color: "var(--lm-text-dim)" }}>
      Harness is offline.{mode?.enabled ? " Voice requests cannot be delivered until it reconnects. You can turn this mode off to use the device agent." : " Reconnect the computer to select an agent and enable this mode."}
    </p>}
    {mode?.pending && <div role="status" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <strong>Delivery is not confirmed</strong>
      <span>The last request may already be running. New voice requests are paused until this is resolved.</span>
      <button type="button" style={control} disabled={busy || !connected} onClick={() => { void mutate("/voice-mode/receipt", {}); }}>Check delivery</button>
      <button type="button" style={control} disabled={busy} onClick={() => {
        if (window.confirm("Resume new voice requests without retrying the previous request? It may still run on Harness.")) {
          void mutate("/voice-mode/resolve", { resolution: "do_not_retry", idempotencyKey: mode.pending?.idempotencyKey });
        }
      }}>Continue without retrying</button>
    </div>}
    {mode?.agentId && <HarnessQuestion key={`${mode.machineId}:${mode.agentId}`} connected={connected} disabled={unavailable || Boolean(mode.pending)} refresh={refresh} onAnswer={answers => mutate("/voice-mode/answer", answers)} />}
    {(error || pollError || mode?.error) && <p role="alert" style={{ margin: 0, color: "var(--lm-red)" }}>{error || pollError || mode?.error}</p>}
  </section>;
}
