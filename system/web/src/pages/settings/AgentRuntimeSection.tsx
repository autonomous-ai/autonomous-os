import { useEffect, useState } from "react";
import { toast } from "sonner";
import { C, SectionCard } from "@/components/setup/shared";
import { getAgentRuntime, setAgentRuntime } from "@/lib/api";

// Agent-runtime switch (openclaw / hermes / picoclaw / codex / claudecode / opencode).
// Unlike the rest of EditConfig this is NOT part of the form's "Save Changes"
// flow: switching is a heavyweight action that toggles systemd units and
// restarts os-server, so it has its own Switch button hitting
// POST /api/device/agent-runtime directly. The POST only means "accepted" —
// after it, onSwitch polls GET /api/device/agent-runtime until the device
// reports the target runtime (real confirmation) or times out.
//
// Options come from the API (single source = domain.AgentRuntimes); the fallback
// list mirrors it only if the fetch fails.
const FALLBACK = ["openclaw", "hermes", "picoclaw", "codex", "claudecode", "opencode"];

const RUNTIME_BLURB: Record<string, string> = {
  openclaw: "OpenClaw — persistent WebSocket gateway (default).",
  hermes: "Hermes — local HTTP+SSE agent server (Nous Research).",
  picoclaw: "PicoClaw — lightweight Go agent gateway (WebSocket).",
  codex: "Codex — OpenAI Codex CLI behind the os-server bridge (WebSocket).",
  claudecode: "Claude Code — Anthropic CLI agent behind a local bridge.",
  opencode: "OpenCode — open-source coding agent behind the os-server bridge (WebSocket).",
};

// Display labels for the runtime dropdown / status pill. Values on the wire
// stay lowercase (systemctl unit names / domain.AgentRuntime* constants); only
// the human-facing string is title-cased. Unknown runtimes (any future addition
// the API returns before this table is updated) fall back to capitalising the
// first letter so the UI never shows raw lowercase.
const RUNTIME_LABEL: Record<string, string> = {
  openclaw: "OpenClaw",
  hermes: "Hermes",
  picoclaw: "PicoClaw",
  codex: "Codex",
  claudecode: "Claude Code",
  opencode: "OpenCode",
};
const displayRuntime = (v: string): string =>
  RUNTIME_LABEL[v] ?? (v ? v[0].toUpperCase() + v.slice(1) : v);

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
      `Switch agentic backend to "${displayRuntime(selected)}"?\n\n` +
      `This stops the other backend and restarts os-server — the device will ` +
      `be briefly unavailable while it reconnects.`,
    )) return;

    setSwitching(true);
    const target = selected;
    try {
      // POST returns 200 = "accepted" immediately; the switch itself runs in the
      // background (may run install.sh on a first switch — minutes, not seconds).
      await setAgentRuntime(target);
    } catch (err) {
      // A different tab/client may already be switching. This is a definitive
      // rejection, unlike a dropped connection during the expected os-server restart.
      if ((err as Error & { status?: number }).status === 409) {
        toast.error("Another runtime switch is already in progress. Wait for it to finish before trying again.");
        setSwitching(false);
        return;
      }
      // os-server may restart before the response lands; a dropped connection
      // here usually means the switch WAS accepted — the poll below finds out.
    }
    toast.message(`Switching to ${displayRuntime(target)} — waiting for the device to confirm…`);

    // config.agent_runtime is only persisted AFTER switch-runtime lands (a failed
    // switch rolls back and keeps the old value), so GET /device/agent-runtime is
    // the source of truth. Poll it until it reports the target: GET errors are the
    // os-server restart window, the old runtime means install still running (or
    // rolled back — indistinguishable until timeout). First-time installs download
    // from the CDN, hence the generous deadline.
    const deadline = Date.now() + 5 * 60_000;
    let landed = false;
    let lastSeen = "";
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 3000));
      try {
        const r = await getAgentRuntime();
        lastSeen = r.current;
        if (r.current === target) { landed = true; break; }
      } catch { /* os-server restarting — keep polling */ }
    }

    if (landed) {
      setCurrent(target);
      toast.success(`Switched to ${displayRuntime(target)} — backend is active.`);
    } else {
      // Timed out: reflect whatever the device actually reports instead of the
      // optimistic value, so a rollback is visible without a page reload.
      if (lastSeen) { setCurrent(lastSeen); setSelected(lastSeen); }
      toast.error(
        lastSeen && lastSeen !== target
          ? `Switch to ${displayRuntime(target)} not confirmed — device still reports "${displayRuntime(lastSeen)}" (likely failed and rolled back; check journalctl -u os-runtime-switch).`
          : `Switch to ${displayRuntime(target)} not confirmed within 5 minutes — reload this page to re-check.`,
      );
    }
    setSwitching(false);
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
              Backend (active: <span style={{ color: C.amber }}>{current ? displayRuntime(current) : "?"}</span>)
            </label>
            <select
              id="agent_runtime"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              disabled={switching}
              style={selectStyle}
            >
              {options.map((o) => <option key={o} value={o}>{displayRuntime(o)}</option>)}
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
            {switching ? "Switching…" : selected === current ? "Active" : `Switch to ${displayRuntime(selected)}`}
          </button>
        </>
      )}
    </SectionCard>
  );
}
