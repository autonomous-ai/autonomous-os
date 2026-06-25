import { useEffect, useState } from "react";
import { toast } from "sonner";
import { C, SectionCard } from "@/components/setup/shared";
import { getAgentRuntime, setAgentRuntime } from "@/lib/api";

// Agent-runtime switch (openclaw ⇄ hermes). Unlike the rest of EditConfig this
// is NOT part of the form's "Save Changes" flow: switching is a heavyweight
// action that toggles systemd units and restarts os-server, so it has its own
// Switch button hitting POST /api/device/agent-runtime directly. After a switch
// the device restarts os-server, dropping this connection — we surface that as
// "reconnecting" rather than an error.
//
// Options come from the API (single source = domain.AgentRuntimes); the fallback
// list mirrors it only if the fetch fails.
const FALLBACK = ["openclaw", "hermes"];

const RUNTIME_BLURB: Record<string, string> = {
  openclaw: "OpenClaw — persistent WebSocket gateway (default).",
  hermes: "Hermes — local HTTP+SSE agent server (Nous Research).",
};

const selectStyle = {
  width: "100%", boxSizing: "border-box" as const,
  background: C.surface, border: `1px solid ${C.border}`,
  borderRadius: 7, padding: "8px 11px",
  fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
};
const labelStyle = { display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 };

export function AgentRuntimeSection({ active }: { active: boolean }) {
  const [current, setCurrent] = useState<string>("");
  const [options, setOptions] = useState<string[]>(FALLBACK);
  const [selected, setSelected] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState(false);

  useEffect(() => {
    getAgentRuntime()
      .then((r) => {
        setCurrent(r.current);
        setSelected(r.current);
        if (r.options?.length) setOptions(r.options);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  async function onSwitch() {
    if (selected === current || switching) return;
    if (!window.confirm(
      `Switch agentic backend to "${selected}"?\n\n` +
      `This stops the other backend and restarts os-server — the device will ` +
      `be briefly unavailable while it reconnects.`,
    )) return;

    setSwitching(true);
    try {
      await setAgentRuntime(selected);
      setCurrent(selected);
      toast.success(`Switching to ${selected} — device is restarting, reconnecting…`);
    } catch (err) {
      // os-server may restart before the response lands; a dropped connection
      // here usually means the switch WAS accepted. Surface it softly.
      toast.message(
        err instanceof Error && err.message
          ? `Switch sent (${err.message}) — if the device restarts, it took effect.`
          : "Switch sent — device may be restarting.",
      );
    } finally {
      setSwitching(false);
    }
  }

  return (
    <SectionCard id="runtime" title="Agent Runtime" active={active}>
      {loading ? (
        <div style={{ fontSize: 12, color: C.textMuted }}>Loading…</div>
      ) : (
        <>
          <div style={{ fontSize: 11.5, color: C.textDim, marginBottom: 12, lineHeight: 1.6 }}>
            The swappable agentic backend that runs the device's brain. Switching
            stops the other backend and restarts os-server.
          </div>

          <div style={{ marginBottom: 6 }}>
            <label htmlFor="agent_runtime" style={labelStyle}>
              Backend (active: <span style={{ color: C.amber }}>{current || "?"}</span>)
            </label>
            <select
              id="agent_runtime"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              disabled={switching}
              style={selectStyle}
            >
              {options.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </div>

          {RUNTIME_BLURB[selected] && (
            <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 14 }}>
              {RUNTIME_BLURB[selected]}
            </div>
          )}

          <button
            type="button"
            onClick={onSwitch}
            disabled={switching || selected === current}
            style={{
              padding: "7px 18px", borderRadius: 7, fontSize: 12, fontWeight: 600,
              border: "none",
              cursor: switching || selected === current ? "not-allowed" : "pointer",
              background: switching || selected === current ? C.surface : C.amber,
              color: switching || selected === current ? C.textMuted : "#0C0B09",
              opacity: switching || selected === current ? 0.6 : 1,
              transition: "all 0.15s",
            }}
          >
            {switching ? "Switching…" : selected === current ? "Active" : `Switch to ${selected}`}
          </button>
        </>
      )}
    </SectionCard>
  );
}
