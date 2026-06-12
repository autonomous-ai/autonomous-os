import { useCallback, useEffect, useRef, useState } from "react";
import { Pencil, Trash2, History, ChevronDown, ChevronRight, X } from "lucide-react";
import { S } from "./styles";
import { hwUrl } from "@/lib/api";
import { HW } from "./types";
import type { FaceOwnersDetail } from "./types";
import { UserTimelineModal } from "./UserTimelineModal";
import { usePolling } from "../../hooks/usePolling";

interface CooldownEntry {
  person_id: string;
  kind: string;
  last_seen_ago: number;
  cooldown_remaining: number;
  cooldown_total: number;
}
interface CooldownState {
  owners: CooldownEntry[];
  strangers: CooldownEntry[];
  owners_forget_s: number;
  strangers_forget_s: number;
}

function fmtCountdown(s: number): string {
  if (s <= 0) return "ready";
  if (s < 60) return `${Math.ceil(s)}s`;
  const m = Math.floor(s / 60);
  const sec = Math.ceil(s % 60);
  return sec > 0 ? `${m}m ${sec}s` : `${m}m`;
}

function fmtAgo(mtime: number): string {
  const diff = Date.now() / 1000 - mtime;
  if (diff < 60) return `${Math.max(1, Math.floor(diff))}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

interface StrangerSample {
  filename: string;
  size_bytes: number;
  mtime: number;
}
interface StrangerCluster {
  hash: string;
  sample_count: number;
  latest_mtime: number;
  samples: StrangerSample[];
}
interface StrangersData {
  total: number;
  clusters: StrangerCluster[];
}

// Familiar-stranger threshold mirrors the device's _FAMILIAR_VISIT_THRESHOLD.
// At this count the device pushes an enroll prompt to the agent (one-shot).
const FAMILIAR_VISIT_THRESHOLD = 2;

interface FaceStrangerStat {
  stranger_id: string;
  count: number;
  first_seen: string;
  last_seen: string;
}

function fmtIsoAgo(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const diff = Date.now() / 1000 - t / 1000;
  if (diff < 60) return `${Math.max(1, Math.floor(diff))}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export function FaceOwnersSection() {
  const [data, setData] = useState<FaceOwnersDetail | null>(null);
  const [error, setError] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // Cooldown state (strangers/friends forget countdown — unrelated to
  // current_user; /face/cooldowns is purely a debug view of detection state).
  const [cooldowns, setCooldowns] = useState<CooldownState | null>(null);
  const [cdError, setCdError] = useState(false);
  const [resetting, setResetting] = useState(false);

  // Current user (effective user the device sees right now). Polled separately
  // from /face/current-user — this is the source used by the OS-server handler,
  // activity logging, and the "Here now" UI.
  const [currentUser, setCurrentUser] = useState<string>("");

  // Enroll form state
  const [showEnroll, setShowEnroll] = useState(false);
  const [enrollName, setEnrollName] = useState("");
  const [enrollTgUsername, setEnrollTgUsername] = useState("");
  const [enrollTgId, setEnrollTgId] = useState("");
  const [enrollFile, setEnrollFile] = useState<File | null>(null);
  const [enrolling, setEnrolling] = useState(false);
  const [enrollError, setEnrollError] = useState("");

  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Delete state
  const [deleting, setDeleting] = useState<string | null>(null);
  const [deletingPhoto, setDeletingPhoto] = useState<string | null>(null); // "label/filename"

  // Timeline modal state
  const [timelineUser, setTimelineUser] = useState<string | null>(null);

  // Person card expand state — cards start collapsed so the grid stays dense.
  // Auto-expands the currently-active user the first time it appears.
  const [expandedPerson, setExpandedPerson] = useState<Record<string, boolean>>({});
  // Tracks which card is hovered so its action buttons fade in (cleaner UX
  // than a permanent row of icons cluttering every card).
  const [hoveredPerson, setHoveredPerson] = useState<string | null>(null);
  // Tracks the hovered photo thumbnail so only its delete button shows —
  // identified by "label/filename".
  const [hoveredPhoto, setHoveredPhoto] = useState<string | null>(null);

  // Unknown voice clusters (/voice/strangers).
  const [strangers, setStrangers] = useState<StrangersData | null>(null);
  const [strangersError, setStrangersError] = useState(false);
  const [expandedCluster, setExpandedCluster] = useState<Record<string, boolean>>({});
  const [deletingCluster, setDeletingCluster] = useState<string | null>(null);
  const [deletingStrangerFile, setDeletingStrangerFile] = useState<string | null>(null); // "hash/filename"

  // Face stranger visit stats (/face/stranger-stats). The device tracks each
  // unrecognized face's visit count and surfaces a familiar-stranger enroll
  // prompt to the agent when count crosses FAMILIAR_VISIT_THRESHOLD.
  const [faceStrangers, setFaceStrangers] = useState<FaceStrangerStat[] | null>(null);
  const [faceStrangersError, setFaceStrangersError] = useState(false);

  // Folder toggle state: "label:mood" => expanded
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  // File preview state: { label, path, content, loading }
  const [preview, setPreview] = useState<{ label: string; path: string; content: string } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  const toggleDir = (key: string) => setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  // Audio playback state
  const [playingAudio, setPlayingAudio] = useState<string | null>(null); // "label/path"
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const playAudio = (label: string, filepath: string) => {
    const key = `${label}/${filepath}`;
    if (playingAudio === key) {
      audioRef.current?.pause();
      setPlayingAudio(null);
      return;
    }
    if (audioRef.current) audioRef.current.pause();
    const audio = new Audio(`${HW}/face/file/${label}/${filepath}`);
    audio.onended = () => setPlayingAudio(null);
    audio.onerror = () => setPlayingAudio(null);
    audio.play().catch(() => setPlayingAudio(null));
    audioRef.current = audio;
    setPlayingAudio(key);
  };

  const downloadFile = (label: string, filepath: string) => {
    const a = document.createElement("a");
    a.href = `${HW}/face/file/${label}/${filepath}`;
    a.download = filepath.split("/").pop() || filepath;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  const openFile = async (label: string, filepath: string) => {
    const isImg = /\.(jpg|jpeg|png|bmp)$/i.test(filepath);
    if (isImg) {
      window.open(
        hwUrl(`/face/photo/${encodeURIComponent(label)}/${encodeURIComponent(filepath)}`),
        "_blank",
        "noopener,noreferrer",
      );
      return;
    }
    const isAudio = /\.(wav|mp3|ogg|webm)$/i.test(filepath);
    if (isAudio) {
      playAudio(label, filepath);
      return;
    }
    const isText = /\.(json|jsonl|txt|log|md|csv|yaml|yml|py|js|ts|tsx)$/i.test(filepath);
    if (!isText) {
      downloadFile(label, filepath);
      return;
    }
    // Already showing this file? close it
    if (preview?.label === label && preview?.path === filepath) {
      setPreview(null);
      return;
    }
    setPreviewLoading(true);
    try {
      const res = await fetch(`${HW}/face/file/${label}/${filepath}`);
      const text = await res.text();
      let content = text;
      if (/\.json$/i.test(filepath)) {
        try { content = JSON.stringify(JSON.parse(text), null, 2); } catch { /* leave raw */ }
      }
      setPreview({ label, path: filepath, content });
    } catch {
      setPreview({ label, path: filepath, content: "(failed to load)" });
    } finally {
      setPreviewLoading(false);
    }
  };

  const refresh = useCallback(async () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const r = await fetch(`${HW}/face/owners`, { signal: ctrl.signal }).then((x) => x.json());
      if (ctrl.signal.aborted) return;
      setData({ enrolled_count: r.enrolled_count ?? 0, persons: r.persons ?? [] });
      setError(false);
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      setError(true);
    }
  }, []);

  useEffect(() => {
    return () => { abortRef.current?.abort(); };
  }, []);

  usePolling(async (signal) => {
    // Delegate to refresh(), but we can't pass the signal because refresh
    // uses its own AbortController. The usePolling timeout will still fire
    // its own abort — refresh's internal controller handles staleness.
    void signal;
    await refresh();
  }, 10_000, { timeoutMs: 8000 });

  const refreshFaceState = useCallback(async (signal?: AbortSignal) => {
    const [cdRes, cuRes] = await Promise.allSettled([
      fetch(`${HW}/face/cooldowns`, { signal }),
      fetch(`${HW}/face/current-user`, { signal }),
    ]);
    if (cdRes.status === "fulfilled" && cdRes.value.ok) {
      setCooldowns(await cdRes.value.json());
      setCdError(false);
    } else {
      setCdError(true);
    }
    if (cuRes.status === "fulfilled" && cuRes.value.ok) {
      const j = await cuRes.value.json();
      setCurrentUser(typeof j?.current_user === "string" ? j.current_user : "");
    }
  }, []);

  usePolling(async (signal) => { await refreshFaceState(signal); }, 5000);

  const refreshStrangers = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await fetch(`${HW}/voice/strangers`, { signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const j = await res.json();
      setStrangers({
        total: j.total ?? 0,
        clusters: Array.isArray(j.clusters) ? j.clusters : [],
      });
      setStrangersError(false);
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      setStrangersError(true);
    }
  }, []);

  usePolling(async (signal) => { await refreshStrangers(signal); }, 15_000);

  const refreshFaceStrangers = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await fetch(`${HW}/face/stranger-stats`, { signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const j = (await res.json()) as Record<string, { count?: number; first_seen?: string; last_seen?: string }>;
      const rows: FaceStrangerStat[] = Object.entries(j ?? {}).map(([sid, v]) => ({
        stranger_id: sid,
        count: v?.count ?? 0,
        first_seen: v?.first_seen ?? "",
        last_seen: v?.last_seen ?? "",
      }));
      // Newest activity first.
      rows.sort((a, b) => Date.parse(b.last_seen || "") - Date.parse(a.last_seen || ""));
      setFaceStrangers(rows);
      setFaceStrangersError(false);
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      setFaceStrangersError(true);
    }
  }, []);

  usePolling(async (signal) => { await refreshFaceStrangers(signal); }, 15_000);

  const handleDeleteCluster = async (hash: string, sampleCount: number) => {
    if (!confirm(`Delete cluster ${hash} (${sampleCount} sample${sampleCount !== 1 ? "s" : ""}) and its centroid?`)) return;
    setDeletingCluster(hash);
    try {
      const res = await fetch(`${HW}/voice/strangers/${hash}`, { method: "DELETE" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        alert(`Delete failed: ${err.detail ?? res.status}`);
      }
      await refreshStrangers();
    } catch (e) {
      alert(`Delete failed: ${(e as Error).message}`);
    } finally {
      setDeletingCluster(null);
    }
  };

  const handleDeleteStrangerFile = async (hash: string, filename: string) => {
    if (!confirm(`Delete sample ${filename} from ${hash}?`)) return;
    const key = `${hash}/${filename}`;
    setDeletingStrangerFile(key);
    try {
      const res = await fetch(`${HW}/voice/strangers/${hash}/${encodeURIComponent(filename)}`, { method: "DELETE" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        alert(`Delete failed: ${err.detail ?? res.status}`);
      }
      await refreshStrangers();
    } catch (e) {
      alert(`Delete failed: ${(e as Error).message}`);
    } finally {
      setDeletingStrangerFile(null);
    }
  };

  const handleResetCooldowns = async () => {
    setResetting(true);
    try {
      await fetch(`${HW}/face/cooldowns/reset`, { method: "POST" });
      await refreshFaceState();
    } catch {
      // ignore
    } finally {
      setResetting(false);
    }
  };

  const handleEnroll = async () => {
    if (!enrollFile || !enrollName.trim()) return;
    setEnrolling(true);
    setEnrollError("");
    try {
      const base64 = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const result = reader.result as string;
          resolve(result.split(",")[1]); // strip "data:image/...;base64,"
        };
        reader.onerror = () => reject(new Error("Failed to read file"));
        reader.readAsDataURL(enrollFile);
      });
      const res = await fetch(`${HW}/face/enroll`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          image_base64: base64,
          label: enrollName.trim().toLowerCase(),
          ...(enrollTgUsername.trim() ? { telegram_username: enrollTgUsername.trim() } : {}),
          ...(enrollTgId.trim() ? { telegram_id: enrollTgId.trim() } : {}),
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Unknown error" }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      setShowEnroll(false);
      setEnrollName("");
      setEnrollTgUsername("");
      setEnrollTgId("");
      setEnrollFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      refresh();
    } catch (e) {
      setEnrollError((e as Error).message);
    } finally {
      setEnrolling(false);
    }
  };

  const handleRemove = async (label: string) => {
    if (!confirm(`Remove "${label}" and all their photos?`)) return;
    setDeleting(label);
    try {
      await fetch(`${HW}/face/remove`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label }),
      });
      refresh();
    } catch {
      // ignore
    } finally {
      setDeleting(null);
    }
  };

  const handleRename = async (oldLabel: string) => {
    const next = prompt(`Rename "${oldLabel}" to:`, oldLabel);
    if (next == null) return;
    const newLabel = next.trim().toLowerCase();
    if (!newLabel || newLabel === oldLabel) return;
    if (!/^[a-z0-9_-]+$/.test(newLabel)) {
      alert("Name can only contain lowercase letters, digits, _ and -");
      return;
    }
    try {
      const resp = await fetch(`${HW}/users/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old_label: oldLabel, new_label: newLabel }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        alert(`Rename failed: ${data.detail ?? resp.statusText}`);
        return;
      }
      refresh();
    } catch (e) {
      alert(`Rename failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const handleRemovePhoto = async (label: string, filename: string) => {
    if (!confirm(`Remove photo "${filename}" from ${label}?`)) return;
    const key = `${label}/${filename}`;
    setDeletingPhoto(key);
    try {
      await fetch(`${HW}/face/photo/remove`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label, filename }),
      });
      refresh();
    } catch {
      // ignore
    } finally {
      setDeletingPhoto(null);
    }
  };

  // Voice sample delete — only audio files. JSON/NPY (metadata, embedding
  // cache) are protected because deleting them silently corrupts the
  // speaker_recognizer profile. Backend OS server /api/voice/file/remove
  // re-enrolls from remaining samples to refresh the embedding.
  const handleRemoveVoiceFile = async (label: string, filename: string) => {
    if (!confirm(`Remove voice sample "${filename}" from ${label}?`)) return;
    const key = `${label}/voice/${filename}`;
    setDeletingPhoto(key);
    try {
      await fetch(`/api/voice/file/remove`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: label, file: filename }),
      });
      refresh();
    } catch {
      // ignore
    } finally {
      setDeletingPhoto(null);
    }
  };

  const inputStyle: React.CSSProperties = {
    fontSize: 12,
    padding: "6px 10px",
    borderRadius: 6,
    background: "var(--lm-surface)",
    border: "1px solid var(--lm-border)",
    color: "var(--lm-text)",
    outline: "none",
    width: "100%",
  };

  const btnStyle: React.CSSProperties = {
    fontSize: 10,
    padding: "4px 12px",
    borderRadius: 6,
    border: "1px solid var(--lm-border)",
    cursor: "pointer",
    fontWeight: 600,
  };

  // Shared "header strip" — tinted background + bottom border, extended to span
  // the full card width via negative margins (S.card has 16px padding) so the
  // header visually separates from card body.
  const cardHeaderStrip: React.CSSProperties = {
    display: "flex", justifyContent: "space-between", alignItems: "center",
    margin: "-16px -16px 12px -16px",
    padding: "10px 14px",
    background: "color-mix(in srgb, var(--lm-text) 5%, transparent)",
    borderBottom: "1px solid var(--lm-border)",
    borderTopLeftRadius: 12,
    borderTopRightRadius: 12,
  };

  // Square icon button — used for the per-person action row (Edit / Timeline /
  // Delete / Expand) so each is the same compact size regardless of label width.
  const iconBtnStyle: React.CSSProperties = {
    width: 26, height: 26,
    display: "inline-flex", alignItems: "center", justifyContent: "center",
    padding: 0, borderRadius: 5,
    background: "var(--lm-surface)",
    color: "var(--lm-text-dim)",
    border: "1px solid var(--lm-border)",
    cursor: "pointer",
    fontSize: 13,
    lineHeight: 1,
  };

  const allCooldownEntries = [
    ...(cooldowns?.owners ?? []),
    ...(cooldowns?.strangers ?? []),
  ];
  const hasActiveCooldowns = allCooldownEntries.some((e) => e.cooldown_remaining > 0);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* Summary — single-row strip: label + counts + "here now" pill + actions.
          Everything inline so it reads at a glance and doesn't waste vertical space. */}
      <div style={{ ...S.card, padding: "10px 14px", display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <div style={{ ...S.cardLabel, marginBottom: 0 }}>Users</div>

        {error ? (
          <span style={{ fontSize: 11, color: "var(--lm-red)" }}>
            User recognizer unavailable
          </span>
        ) : data ? (
          <span style={{ fontSize: 12, color: "var(--lm-text-dim)" }}>
            <span style={{ fontWeight: 700, color: "var(--lm-amber)" }}>{data.enrolled_count}</span>
            {" "}enrolled
          </span>
        ) : (
          <span style={{ fontSize: 11, color: "var(--lm-text-muted)" }}>Loading…</span>
        )}

        {currentUser && (
          <span style={{
            fontSize: 11,
            padding: "3px 9px",
            borderRadius: 4,
            background: currentUser === "unknown" ? "rgba(148,163,184,0.15)" : "rgba(45,212,191,0.18)",
            color: currentUser === "unknown" ? "var(--lm-text-muted)" : "var(--lm-teal)",
            fontWeight: 700,
            textTransform: "capitalize",
            letterSpacing: "0.03em",
          }}>
            ● {currentUser}
          </span>
        )}

        <span style={{ flex: 1 }} />

        <div style={{ display: "flex", gap: 6 }}>
          <button
            onClick={() => setShowEnroll(!showEnroll)}
            style={{
              ...btnStyle,
              background: showEnroll ? "var(--lm-amber-dim)" : "var(--lm-surface)",
              color: showEnroll ? "var(--lm-amber)" : "var(--lm-text-dim)",
            }}
          >
            + Enroll
          </button>
          <button
            onClick={refresh}
            style={{ ...btnStyle, background: "var(--lm-surface)", color: "var(--lm-text-dim)" }}
          >
            ↻
          </button>
        </div>
      </div>

      {/* Enroll form */}
      {showEnroll && (
        <div style={S.card}>
          <div style={{ ...S.cardLabel, marginBottom: 14 }}>Add New User</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <input
              type="text"
              placeholder="Name"
              value={enrollName}
              onChange={(e) => setEnrollName(e.target.value)}
              style={inputStyle}
            />
            <input
              type="text"
              placeholder="Telegram username (optional)"
              value={enrollTgUsername}
              onChange={(e) => setEnrollTgUsername(e.target.value)}
              style={inputStyle}
            />
            <input
              type="text"
              placeholder="Telegram ID (optional)"
              value={enrollTgId}
              onChange={(e) => setEnrollTgId(e.target.value)}
              style={inputStyle}
            />
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              onChange={(e) => setEnrollFile(e.target.files?.[0] ?? null)}
              style={{ ...inputStyle, padding: "4px 6px" }}
            />
            {enrollError && (
              <div style={{ fontSize: 11, color: "var(--lm-red)" }}>{enrollError}</div>
            )}
            <button
              onClick={handleEnroll}
              disabled={enrolling || !enrollFile || !enrollName.trim()}
              style={{
                ...btnStyle,
                padding: "7px 14px",
                fontSize: 12,
                background: enrolling || !enrollFile || !enrollName.trim()
                  ? "var(--lm-surface)"
                  : "var(--lm-amber-dim)",
                color: enrolling || !enrollFile || !enrollName.trim()
                  ? "var(--lm-text-muted)"
                  : "var(--lm-amber)",
                cursor: enrolling || !enrollFile || !enrollName.trim() ? "not-allowed" : "pointer",
              }}
            >
              {enrolling ? "Adding..." : "Add User"}
            </button>
          </div>
        </div>
      )}

      {/* Person cards */}
      {data && data.persons.length > 0 && (
        <div className="lm-grid-4">
          {data.persons.map((person) => {
            const isCurrent = !!currentUser && currentUser === person.label;
            // Expand active user by default so the most-relevant card is open;
            // others stay collapsed until clicked.
            const isExpanded = expandedPerson[person.label] ?? isCurrent;
            const cardStyle: React.CSSProperties = isCurrent
              ? {
                  ...S.card,
                  border: "2px solid var(--lm-teal)",
                  boxShadow: "0 0 12px rgba(45,212,191,0.25)",
                }
              : S.card;
            return (
            <div
              key={person.label}
              style={cardStyle}
              onMouseEnter={() => setHoveredPerson(person.label)}
              onMouseLeave={() => setHoveredPerson((cur) => (cur === person.label ? null : cur))}
            >

              {/* Row 1 — name + actions. Visually a header strip with its own
                  background + bottom border, extended to span the full card
                  width via negative margins (S.card has 16px padding).
                  Clicking it toggles expand/collapse. */}
              <div
                onClick={() => setExpandedPerson((p) => ({ ...p, [person.label]: !isExpanded }))}
                style={{
                  display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap",
                  cursor: "pointer",
                  margin: "-16px -16px 12px -16px",
                  padding: "10px 14px",
                  background: "color-mix(in srgb, var(--lm-text) 5%, transparent)",
                  borderBottom: "1px solid var(--lm-border)",
                  borderTopLeftRadius: 12,
                  borderTopRightRadius: 12,
                }}
              >
                <div style={{
                  fontSize: 14, fontWeight: 700,
                  color: "var(--lm-amber)",
                  textTransform: "capitalize",
                }}>
                  {person.label}
                </div>
                {isCurrent && (
                  <span style={{
                    fontSize: 9, padding: "2px 6px", borderRadius: 4,
                    background: "var(--lm-teal)", color: "#0b1220",
                    fontWeight: 700, letterSpacing: 0.5,
                  }}>● HERE NOW</span>
                )}
                <span style={{ flex: 1 }} />
                {/* Actions: Delete / Edit / Timeline / expand toggle.
                    Edit is hidden for the special "unknown" bucket since it
                    isn't a real user that can be renamed. */}
                {(() => {
                  const isHovered = hoveredPerson === person.label;
                  // Keep hovered buttons fully visible; fade out (but keep
                  // interactive) when not hovered so the row stays the same
                  // height — avoids layout shift.
                  const hoverStyle: React.CSSProperties = {
                    opacity: isHovered ? 1 : 0,
                    pointerEvents: isHovered ? "auto" : "none",
                    transition: "opacity 0.15s ease",
                  };
                  return (
                    <>
                      {person.label !== "unknown" && (
                        <button
                          onClick={(e) => { e.stopPropagation(); handleRename(person.label); }}
                          title="Rename"
                          aria-label="Rename"
                          style={{ ...iconBtnStyle, ...hoverStyle }}
                        ><Pencil size={14} /></button>
                      )}
                      <button
                        onClick={(e) => { e.stopPropagation(); setTimelineUser(person.label); }}
                        title="Timeline"
                        aria-label="Timeline"
                        style={{
                          ...iconBtnStyle,
                          background: "color-mix(in srgb, var(--lm-blue) 15%, transparent)",
                          color: "var(--lm-blue)",
                          border: "1px solid color-mix(in srgb, var(--lm-blue) 30%, transparent)",
                          ...hoverStyle,
                        }}
                      ><History size={14} /></button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleRemove(person.label); }}
                        disabled={deleting === person.label}
                        title="Delete user"
                        aria-label="Delete user"
                        style={{
                          ...iconBtnStyle,
                          background: "color-mix(in srgb, var(--lm-red) 12%, transparent)",
                          color: "var(--lm-red)",
                          border: "1px solid color-mix(in srgb, var(--lm-red) 35%, transparent)",
                          cursor: deleting === person.label ? "not-allowed" : "pointer",
                          opacity: deleting === person.label ? 0.5 : (isHovered ? 1 : 0),
                          pointerEvents: isHovered ? "auto" : "none",
                          transition: "opacity 0.15s ease",
                        }}
                      >{deleting === person.label ? "…" : <Trash2 size={14} />}</button>
                      {/* Inline chevron indicator — non-interactive, just a visual
                          hint that the card is clickable to expand. Always visible. */}
                      <span style={{
                        display: "inline-flex", alignItems: "center", justifyContent: "center",
                        width: 18, height: 18, color: "var(--lm-text-muted)",
                      }}>
                        {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      </span>
                    </>
                  );
                })()}
              </div>

              {(person.telegram_username || person.telegram_id) && (
                <div style={{ fontSize: 10, color: "var(--lm-text-muted)", marginBottom: 12 }}>
                  {person.telegram_username && <span>@{person.telegram_username}</span>}
                  {person.telegram_username && person.telegram_id && <span> · </span>}
                  {person.telegram_id && <span>ID: {person.telegram_id}</span>}
                </div>
              )}

              {/* Row 2 — metric tokens (counts of photos/mood/wb/etc.) */}
              {(() => {
                const audioCount = person.voice_samples?.filter((f) => /\.(wav|mp3|ogg)$/i.test(f)).length ?? 0;
                  // Compact metric strip — short tokens, color-coded by category,
                  // tooltip on hover for the full label. Keeps the person card
                  // dense even when 4 cards sit on one row.
                  const tags: Array<{ n: number | string; label: string; full: string; color: string }> = [
                    { n: person.photo_count, label: "photos", full: `${person.photo_count} face photos`, color: "var(--lm-amber)" },
                  ];
                  if (person.mood_days?.length)              tags.push({ n: person.mood_days.length,             label: "mood",     full: `${person.mood_days.length} mood days`,             color: "var(--lm-green)"  });
                  if (person.wellbeing_days?.length)         tags.push({ n: person.wellbeing_days.length,        label: "wb",       full: `${person.wellbeing_days.length} wellbeing days`,    color: "var(--lm-blue)"   });
                  if (person.music_suggestion_days?.length)  tags.push({ n: person.music_suggestion_days.length, label: "music",    full: `${person.music_suggestion_days.length} music suggestion days`, color: "var(--lm-purple)" });
                  if (person.posture_days?.length)           tags.push({ n: person.posture_days.length,          label: "posture",  full: `${person.posture_days.length} posture days`,        color: "var(--lm-cyan, #06b6d4)" });
                  if (person.audio_history_days?.length)     tags.push({ n: person.audio_history_days.length,    label: "audio",    full: `${person.audio_history_days.length} audio history days`, color: "var(--lm-blue)" });
                  if (person.habit_patterns)                 tags.push({ n: "✓",                                  label: "habit",    full: "Habit patterns recorded",                            color: "var(--lm-amber)"  });
                  if (audioCount > 0)                        tags.push({ n: audioCount,                          label: "voice",    full: `${audioCount} voice samples`,                       color: "var(--lm-purple)" });

                  return (
                    <div style={{ display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap", fontSize: 10, marginBottom: 8 }}>
                      {tags.map((t, i) => (
                        <span
                          key={i}
                          title={t.full}
                          style={{
                            display: "inline-flex", alignItems: "center", gap: 3,
                            padding: "2px 6px", borderRadius: 4,
                            background: `color-mix(in srgb, ${t.color} 14%, transparent)`,
                            color: t.color,
                            fontWeight: 600,
                            fontFamily: "monospace",
                            letterSpacing: "0.02em",
                          }}
                        >
                          <strong>{t.n}</strong> {t.label}
                        </span>
                      ))}
                    </div>
                  );
                })()}

              {/* Expandable detail section — photos gallery, folder tree, preview.
                  Hidden when card is collapsed to keep the grid dense. */}
              {isExpanded && (<>
              <div style={{
                fontFamily: "monospace",
                fontSize: 11,
                lineHeight: 1.7,
                color: "var(--lm-text-muted)",
              }}>
                {/* Photos gallery — single horizontal row of thumbnails so the
                    person card stays dense. Hover a thumbnail to reveal its ✕
                    delete button. */}
                {person.photos.length > 0 && (
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
                    {person.photos.map((photo) => {
                      const delKey = `${person.label}/${photo}`;
                      const isDeleting = deletingPhoto === delKey;
                      const isHovered = hoveredPhoto === delKey;
                      return (
                        <div
                          key={photo}
                          title={photo}
                          onMouseEnter={() => setHoveredPhoto(delKey)}
                          onMouseLeave={() => setHoveredPhoto((cur) => (cur === delKey ? null : cur))}
                          style={{ position: "relative", width: 56, height: 56 }}
                        >
                          <img
                            src={hwUrl(`/face/photo/${encodeURIComponent(person.label)}/${encodeURIComponent(photo)}`)}
                            style={{
                              width: "100%", height: "100%",
                              objectFit: "cover",
                              borderRadius: 6,
                              border: "1px solid var(--lm-border)",
                              display: "block",
                              cursor: "pointer",
                            }}
                            onClick={() => openFile(person.label, photo)}
                            onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
                          />
                          <button
                            onClick={(e) => { e.stopPropagation(); handleRemovePhoto(person.label, photo); }}
                            disabled={isDeleting}
                            title={`Remove ${photo}`}
                            style={{
                              position: "absolute", top: 3, right: 3,
                              width: 20, height: 20,
                              borderRadius: 5,
                              background: "rgba(0,0,0,0.55)",
                              color: "#fff",
                              border: "none",
                              cursor: isDeleting ? "wait" : "pointer",
                              padding: 0,
                              display: "flex", alignItems: "center", justifyContent: "center",
                              opacity: isDeleting ? 0.5 : (isHovered ? 1 : 0),
                              pointerEvents: isHovered ? "auto" : "none",
                              transition: "opacity 0.15s ease",
                              backdropFilter: "blur(2px)",
                            }}
                          >
                            <X size={12} strokeWidth={2.5} />
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}

                {(() => {
                  const items: { name: string; isDir?: boolean; dirKey?: string; children?: string[]; filePath?: string }[] = [];
                  // Photos render as the gallery above — exclude from tree so the
                  // filename listing doesn't repeat what the thumbnails already show.
                  person.files?.filter((f) => !person.photos.includes(f)).forEach((f) => items.push({ name: f, filePath: f }));
                  if (person.mood_days && person.mood_days.length > 0) {
                    items.push({ name: "mood", isDir: true, dirKey: `${person.label}:mood`, children: person.mood_days.map((d) => `${d}.jsonl`) });
                  }
                  if (person.wellbeing_days && person.wellbeing_days.length > 0) {
                    items.push({ name: "wellbeing", isDir: true, dirKey: `${person.label}:wellbeing`, children: person.wellbeing_days.map((d) => `${d}.jsonl`) });
                  }
                  if (person.music_suggestion_days && person.music_suggestion_days.length > 0) {
                    items.push({ name: "music-suggestions", isDir: true, dirKey: `${person.label}:music-suggestions`, children: person.music_suggestion_days.map((d) => `${d}.jsonl`) });
                  }
                  if (person.posture_days && person.posture_days.length > 0) {
                    items.push({ name: "posture", isDir: true, dirKey: `${person.label}:posture`, children: person.posture_days.map((d) => `${d}.jsonl`) });
                  }
                  if (person.audio_history_days && person.audio_history_days.length > 0) {
                    items.push({ name: "audio_history", isDir: true, dirKey: `${person.label}:audio_history`, children: person.audio_history_days.map((d) => `${d}.jsonl`) });
                  }
                  if (person.habit_patterns) {
                    items.push({ name: "habit", isDir: true, dirKey: `${person.label}:habit`, children: ["patterns.json"] });
                  }
                  if (person.voice_samples && person.voice_samples.length > 0) {
                    items.push({ name: "voice", isDir: true, dirKey: `${person.label}:voice`, children: person.voice_samples });
                  }
                  return items.map((item, i) => {
                    const isLastTop = i === items.length - 1;
                    const prefix = isLastTop ? "\u2514\u2500\u2500 " : "\u251C\u2500\u2500 ";
                    if (item.isDir && item.dirKey) {
                      const isOpen = expanded[item.dirKey] ?? false;
                      return (
                        <div key={item.name}>
                          <span
                            style={{ cursor: "pointer" }}
                            onClick={() => toggleDir(item.dirKey!)}
                          >
                            <span style={{ color: "var(--lm-text-dim)" }}>{prefix}</span>
                            <span style={{ color: "var(--lm-green)" }}>{isOpen ? "\u25BE" : "\u25B8"}</span>
                            <span style={{ color: "var(--lm-green)", fontWeight: 600 }}> {item.name}/</span>
                          </span>
                          {isOpen && item.children?.map((child, ci) => {
                            const childPrefix = isLastTop ? "    " : "\u2502   ";
                            const childBranch = ci === (item.children?.length ?? 0) - 1 ? "\u2514\u2500\u2500 " : "\u251C\u2500\u2500 ";
                            const childPath = `${item.name}/${child}`;
                            const isActive = preview?.label === person.label && preview?.path === childPath;
                            const isChildAudio = /\.(wav|mp3|ogg|webm)$/i.test(child);
                            const audioKey = `${person.label}/${childPath}`;
                            const isPlaying = playingAudio === audioKey;
                            // Per-file delete only for audio in voice/. metadata.json /
                            // .npy stay protected — deleting them corrupts the profile.
                            const canDelete = item.name === "voice" && isChildAudio && person.label !== "unknown";
                            const deleteKey = `${person.label}/voice/${child}`;
                            const isDeleting = deletingPhoto === deleteKey;
                            return (
                              <div key={child} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                                <span
                                  style={{ cursor: "pointer" }}
                                  onClick={() => openFile(person.label, childPath)}
                                >
                                  <span style={{ color: "var(--lm-text-dim)" }}>{childPrefix}{childBranch}</span>
                                  {isChildAudio && (
                                    <span style={{ color: isPlaying ? "var(--lm-amber)" : "var(--lm-purple)", marginRight: 4 }}>
                                      {isPlaying ? "⏸" : "▶"}
                                    </span>
                                  )}
                                  <span style={{
                                    color: isActive || isPlaying ? "var(--lm-amber)" : "inherit",
                                    textDecoration: "underline",
                                    textDecorationStyle: "dotted" as const,
                                    textUnderlineOffset: 3,
                                  }}>{child}</span>
                                </span>
                                {canDelete && (
                                  <span
                                    onClick={(e) => { e.stopPropagation(); handleRemoveVoiceFile(person.label, child); }}
                                    title={`Remove ${child}`}
                                    style={{
                                      cursor: isDeleting ? "wait" : "pointer",
                                      fontSize: 10,
                                      color: "var(--lm-red)",
                                      opacity: isDeleting ? 0.5 : 0.6,
                                      fontWeight: 600,
                                    }}
                                  >✕</span>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      );
                    }
                    const isActive = preview?.label === person.label && preview?.path === item.filePath;
                    return (
                      <div key={item.name} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <span
                          style={{ cursor: "pointer" }}
                          onClick={() => openFile(person.label, item.filePath!)}
                        >
                          <span style={{ color: "var(--lm-text-dim)" }}>{prefix}</span>
                          <span style={{
                            color: isActive ? "var(--lm-amber)" : "inherit",
                            textDecoration: "underline",
                            textDecorationStyle: "dotted" as const,
                            textUnderlineOffset: 3,
                          }}>{item.name}</span>
                        </span>
                      </div>
                    );
                  });
                })()}
              </div>

              {/* File preview */}
              {preview && preview.label === person.label && (
                <div style={{
                  marginTop: 8,
                  padding: "8px 10px",
                  borderRadius: 6,
                  background: "var(--lm-surface)",
                  border: "1px solid var(--lm-border)",
                  fontSize: 10,
                  fontFamily: "monospace",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-all",
                  maxHeight: 200,
                  overflowY: "auto",
                  color: "var(--lm-text)",
                  position: "relative",
                }}>
                  <div style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    marginBottom: 6,
                    paddingBottom: 4,
                    borderBottom: "1px solid var(--lm-border)",
                  }}>
                    <span style={{ color: "var(--lm-amber)", fontWeight: 600 }}>{preview.path}</span>
                    <span
                      style={{ cursor: "pointer", color: "var(--lm-text-muted)", fontSize: 12 }}
                      onClick={() => setPreview(null)}
                    >x</span>
                  </div>
                  {previewLoading ? "Loading..." : preview.content}
                </div>
              )}
              </> )}{/* /isExpanded */}
            </div>
            );
          })}
        </div>
      )}

      {data && data.persons.length === 0 && !showEnroll && (
        <div style={{ ...S.card, textAlign: "center" as const, padding: 32 }}>
          <div style={{ fontSize: 12, color: "var(--lm-text-muted)", fontStyle: "italic" }}>
            No users enrolled yet. Click "+ Enroll" above or send a photo via Telegram.
          </div>
        </div>
      )}

      {/* Bottom row: 3 diagnostic cards side-by-side so we get the same
          horizontal density as Sensing/Analytics, instead of three full-width
          stacks. */}
      <div className="lm-grid-3">

      {/* Unknown Voice Clusters */}
      <div style={S.card}>
        <div style={cardHeaderStrip}>
          <div style={{ ...S.cardLabel, marginBottom: 0 }}>Unknown Voices</div>
          <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
            {strangers ? `${strangers.total} cluster${strangers.total !== 1 ? "s" : ""}` : ""}
          </span>
        </div>

        {strangersError && (
          <div style={{ fontSize: 12, color: "var(--lm-text-muted)", fontStyle: "italic" }}>
            Voice cluster info unavailable (speaker service down?)
          </div>
        )}

        {!strangersError && strangers && strangers.clusters.length === 0 && (
          <div style={{ fontSize: 12, color: "var(--lm-text-muted)", fontStyle: "italic" }}>
            No unknown voices heard yet.
          </div>
        )}

        {!strangersError && strangers && strangers.clusters.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4, maxHeight: 320, overflowY: "auto" }} className="lm-hide-scroll">
            {strangers.clusters.map((cluster) => {
              const isOpen = expandedCluster[cluster.hash] ?? false;
              return (
                <div key={cluster.hash} style={{
                  padding: "5px 9px",
                  borderRadius: 6,
                  background: "var(--lm-surface)",
                  border: "1px solid var(--lm-border)",
                }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <div
                      style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", flex: 1, minWidth: 0 }}
                      onClick={() => setExpandedCluster((p) => ({ ...p, [cluster.hash]: !isOpen }))}
                    >
                      <span style={{ color: "var(--lm-purple)", fontSize: 10 }}>{isOpen ? "▾" : "▸"}</span>
                      <span style={{ fontSize: 11, fontWeight: 600, color: "var(--lm-purple)", fontFamily: "monospace" }}>
                        {cluster.hash}
                      </span>
                      <span style={{ fontSize: 9, color: "var(--lm-purple)", fontWeight: 600 }}>
                        ×{cluster.sample_count}
                      </span>
                      <span style={{ fontSize: 9, color: "var(--lm-text-muted)" }}>
                        · {fmtAgo(cluster.latest_mtime)}
                      </span>
                    </div>
                    <span
                      onClick={(e) => { e.stopPropagation(); if (deletingCluster !== cluster.hash) handleDeleteCluster(cluster.hash, cluster.sample_count); }}
                      title={`Delete cluster ${cluster.hash}`}
                      style={{
                        cursor: deletingCluster === cluster.hash ? "wait" : "pointer",
                        fontSize: 11, color: "var(--lm-red)",
                        opacity: deletingCluster === cluster.hash ? 0.5 : 0.7,
                        fontWeight: 600, flexShrink: 0, padding: "0 4px",
                      }}
                    >
                      {deletingCluster === cluster.hash ? "…" : "✕"}
                    </span>
                  </div>

                  {isOpen && (
                    <div style={{ display: "flex", flexDirection: "column", gap: 3, marginTop: 6 }}>
                      {cluster.samples.map((s) => {
                        const fileKey = `${cluster.hash}/${s.filename}`;
                        const isDeletingFile = deletingStrangerFile === fileKey;
                        return (
                          <div key={s.filename} title={s.filename} style={{
                            display: "flex", alignItems: "center", gap: 6,
                            fontSize: 10, color: "var(--lm-text-muted)", fontFamily: "monospace",
                          }}>
                            <audio
                              controls preload="none"
                              src={hwUrl(`/voice/strangers/audio/${encodeURIComponent(cluster.hash)}/${encodeURIComponent(s.filename)}`)}
                              style={{ height: 22, flexShrink: 0, width: 180 }}
                            />
                            <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              {s.filename}
                            </span>
                            <span style={{ flexShrink: 0, fontSize: 9 }}>
                              {fmtSize(s.size_bytes)} · {fmtAgo(s.mtime)}
                            </span>
                            <span
                              onClick={() => { if (!isDeletingFile) handleDeleteStrangerFile(cluster.hash, s.filename); }}
                              title={`Remove ${s.filename}`}
                              style={{
                                cursor: isDeletingFile ? "wait" : "pointer",
                                fontSize: 11, color: "var(--lm-red)",
                                opacity: isDeletingFile ? 0.5 : 0.7,
                                fontWeight: 600, flexShrink: 0, padding: "0 2px",
                              }}
                            >✕</span>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Unknown Faces (visit stats per stranger_id) */}
      <div style={S.card}>
        <div style={cardHeaderStrip}>
          <div style={{ ...S.cardLabel, marginBottom: 0 }}>Unknown Faces</div>
          <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
            {faceStrangers ? `${faceStrangers.length} stranger${faceStrangers.length !== 1 ? "s" : ""}` : ""}
          </span>
        </div>

        {faceStrangersError && (
          <div style={{ fontSize: 12, color: "var(--lm-text-muted)", fontStyle: "italic" }}>
            Face stranger stats unavailable (sensing not started?)
          </div>
        )}

        {!faceStrangersError && faceStrangers && faceStrangers.length === 0 && (
          <div style={{ fontSize: 12, color: "var(--lm-text-muted)", fontStyle: "italic" }}>
            No unknown faces tracked yet.
          </div>
        )}

        {!faceStrangersError && faceStrangers && faceStrangers.length > 0 && (
          // Local scroll — list can grow unbounded as new strangers are tracked,
          // and the surrounding 3-col row should stay aligned with sibling cards.
          <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 320, overflowY: "auto" }} className="lm-hide-scroll">
            {faceStrangers.map((s) => {
              const familiar = s.count >= FAMILIAR_VISIT_THRESHOLD;
              const accent = familiar ? "var(--lm-amber)" : "var(--lm-red)";
              const accentBg = familiar ? "rgba(251,191,36,0.15)" : "rgba(239,68,68,0.1)";
              return (
                <div key={s.stranger_id} style={{
                  padding: "8px 12px",
                  borderRadius: 8,
                  background: "var(--lm-surface)",
                  border: "1px solid var(--lm-border)",
                }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4, gap: 8, flexWrap: "wrap" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <span style={{
                        fontSize: 12,
                        fontWeight: 700,
                        color: accent,
                        fontFamily: "monospace",
                      }}>
                        {s.stranger_id}
                      </span>
                      <span style={{
                        fontSize: 9,
                        padding: "1px 6px",
                        borderRadius: 4,
                        background: accentBg,
                        color: accent,
                        fontWeight: 600,
                      }}>
                        {s.count} visit{s.count !== 1 ? "s" : ""}
                      </span>
                      {familiar && (
                        <span
                          title={`Visit count ≥ ${FAMILIAR_VISIT_THRESHOLD} (familiar threshold). Lelamp fires the enroll prompt only on the 1→${FAMILIAR_VISIT_THRESHOLD} transition — strangers whose count was already past the threshold before the trigger code was deployed will NOT have been prompted.`}
                          style={{
                            fontSize: 9,
                            padding: "1px 6px",
                            borderRadius: 4,
                            background: "rgba(251,191,36,0.15)",
                            color: "var(--lm-amber)",
                            fontWeight: 700,
                            letterSpacing: 0.3,
                          }}
                        >
                          ● FAMILIAR
                        </span>
                      )}
                    </div>
                    <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
                      last {s.last_seen ? fmtIsoAgo(s.last_seen) : "?"}
                    </span>
                  </div>
                  <div style={{ fontSize: 9, color: "var(--lm-text-muted)" }}>
                    first seen {s.first_seen ? fmtIsoAgo(s.first_seen) : "?"}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Face Recognition Cooldowns */}
      <div style={S.card}>
        <div style={cardHeaderStrip}>
          <div style={{ ...S.cardLabel, marginBottom: 0 }}>Face Recognition</div>
          <button
            onClick={handleResetCooldowns}
            disabled={resetting || !hasActiveCooldowns}
            style={{
              fontSize: 10,
              padding: "4px 12px",
              borderRadius: 6,
              border: "1px solid var(--lm-border)",
              cursor: resetting || !hasActiveCooldowns ? "not-allowed" : "pointer",
              fontWeight: 600,
              background: hasActiveCooldowns ? "var(--lm-amber-dim)" : "var(--lm-surface)",
              color: hasActiveCooldowns ? "var(--lm-amber)" : "var(--lm-text-muted)",
              opacity: resetting ? 0.5 : 1,
            }}
          >
            {resetting ? "Resetting..." : "Reset Cooldowns"}
          </button>
        </div>

        {cdError && (
          <div style={{ fontSize: 12, color: "var(--lm-text-muted)", fontStyle: "italic" }}>
            Cooldown info unavailable
          </div>
        )}

        {!cdError && allCooldownEntries.length === 0 && (
          <div style={{ fontSize: 12, color: "var(--lm-text-muted)", fontStyle: "italic" }}>
            No faces currently tracked
          </div>
        )}

        {!cdError && allCooldownEntries.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 320, overflowY: "auto" }} className="lm-hide-scroll">
            {allCooldownEntries.map((entry) => {
              const pct = entry.cooldown_total > 0
                ? (entry.cooldown_remaining / entry.cooldown_total) * 100
                : 0;
              const kindColor =
                entry.kind === "stranger" ? "var(--lm-red)"
                : "var(--lm-blue)";
              return (
                <div key={`${entry.kind}-${entry.person_id}`} style={{
                  padding: "8px 12px",
                  borderRadius: 8,
                  background: "var(--lm-surface)",
                  border: "1px solid var(--lm-border)",
                }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span style={{
                        fontSize: 12,
                        fontWeight: 600,
                        color: kindColor,
                        textTransform: "capitalize",
                      }}>
                        {entry.person_id}
                      </span>
                      <span style={{
                        fontSize: 9,
                        padding: "1px 6px",
                        borderRadius: 4,
                        background: entry.kind === "stranger" ? "rgba(239,68,68,0.1)" : "rgba(96,165,250,0.15)",
                        color: kindColor,
                        fontWeight: 600,
                      }}>
                        {entry.kind}
                      </span>
                    </div>
                    <span style={{
                      fontSize: 11,
                      fontWeight: 600,
                      fontFamily: "monospace",
                      color: entry.cooldown_remaining > 0 ? "var(--lm-text)" : "var(--lm-green)",
                    }}>
                      {fmtCountdown(entry.cooldown_remaining)}
                    </span>
                  </div>
                  {/* Progress bar */}
                  <div style={{
                    height: 4,
                    borderRadius: 2,
                    background: "var(--lm-border)",
                    overflow: "hidden",
                  }}>
                    <div style={{
                      height: "100%",
                      width: `${pct}%`,
                      borderRadius: 2,
                      background: kindColor,
                      transition: "width 1.5s linear",
                    }} />
                  </div>
                  <div style={{ fontSize: 9, color: "var(--lm-text-muted)", marginTop: 4 }}>
                    seen {Math.round(entry.last_seen_ago)}s ago · next event in {fmtCountdown(entry.cooldown_remaining)}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      </div>{/* /lm-grid-3 bottom row */}

      {timelineUser && (
        <UserTimelineModal user={timelineUser} onClose={() => setTimelineUser(null)} />
      )}
    </div>
  );
}
