import { useCallback, useEffect, useState } from "react";
import {
  FolderTree, Folder, FileText, Loader2, RefreshCw, AlertCircle, ChevronRight,
} from "lucide-react";
import { listInstalledSkills, readSkillFiles } from "@/lib/api";
import type { InstalledSkill, SkillBundleFile } from "@/lib/api";
import { ModalShell } from "./ModalShell";
import { SkillFilesView } from "./SkillFilesView";

// "Manage skills" — the skills present in the ACTIVE agentic runtime's skills
// dir. Two views, deliberately the same shape as Browse skills:
//
//   list   → GET /api/agent/skills        (AgentGateway.ListSkills)
//   detail → GET /api/agent/skills/files  (AgentGateway.ReadSkillFiles)
//
// The detail view is the SAME component the store preview uses
// (SkillFilesView) — the backend returns the same SkillBundleFile[] whether the
// files came out of a downloaded archive or off the runtime's disk.
//
// Everything the runtime has shows up here regardless of how it got there:
// authored, store-installed, role-bundled and OTA-pushed skills all land in the
// same tree. A runtime that can't list skills answers 501 and the message is
// shown inline — an empty list means "provisioned but empty", not "unsupported".

export function ManageSkillsModal({ onClose }: { onClose: () => void }) {
  const [selected, setSelected] = useState<InstalledSkill | null>(null);
  return selected
    // key: a detail view is bound to one skill for its whole lifetime, so its
    // fetch effect never has to reset state for a different name.
    ? <SkillDetail key={selected.name} skill={selected} onBack={() => setSelected(null)} onClose={onClose} />
    : <SkillList onOpen={setSelected} onClose={onClose} />;
}

// ─── List view ───────────────────────────────────────────────────────────────

function SkillList({
  onOpen, onClose,
}: {
  onOpen: (s: InstalledSkill) => void;
  onClose: () => void;
}) {
  const [skills, setSkills] = useState<InstalledSkill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setSkills(await listInstalledSkills());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to list skills");
      setSkills([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const subtitle = loading
    ? "Loading…"
    : error
      ? "Could not read the runtime's skills"
      : `${skills.length} installed on this runtime`;

  return (
    <ModalShell icon={FolderTree} title="Manage skills" subtitle={subtitle} width={640} onClose={onClose}>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 10 }}>
        <button
          type="button"
          className="lm-u-btn"
          onClick={() => void load()}
          disabled={loading}
          title="Reload"
          aria-label="Reload"
          style={{
            width: 32, height: 30, borderRadius: 8, display: "flex",
            alignItems: "center", justifyContent: "center",
          }}
        ><RefreshCw size={13} className={loading ? "lm-spin-ico" : undefined} /></button>
      </div>

      {loading ? (
        <Centered><Loader2 size={18} className="lm-spin-ico" /> Loading skills…</Centered>
      ) : error ? (
        <Centered tone="error"><AlertCircle size={16} /> {error}</Centered>
      ) : skills.length === 0 ? (
        <Centered>No skills installed on this runtime yet.</Centered>
      ) : (
        // Same two-per-row grid as Browse skills.
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))",
          gap: 8,
        }}>
          {skills.map((s) => <SkillRow key={s.name} skill={s} onOpen={() => onOpen(s)} />)}
        </div>
      )}
    </ModalShell>
  );
}

function SkillRow({ skill, onOpen }: { skill: InstalledSkill; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      style={{
        display: "flex", alignItems: "center", gap: 10, width: "100%", textAlign: "left",
        padding: "10px 12px", borderRadius: 10, cursor: "pointer",
        background: "var(--lm-card)", border: "1px solid var(--lm-border)",
        color: "var(--lm-text)", transition: "border-color 0.12s",
      }}
      onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--lm-border-hi)"; }}
      onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--lm-border)"; }}
    >
      <Folder size={15} style={{ color: "var(--lm-amber)", flexShrink: 0 }} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 600 }}>/{skill.name}</div>
        {skill.description && (
          <div style={{
            fontSize: 11.5, color: "var(--lm-text-dim)", marginTop: 3, lineHeight: 1.45,
            display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden",
          }}>{skill.description}</div>
        )}
        <div style={{ fontSize: 10, color: "var(--lm-text-muted)", marginTop: 6 }}>
          {countFiles(skill)} file{countFiles(skill) === 1 ? "" : "s"}
        </div>
      </div>
      <ChevronRight size={15} style={{ color: "var(--lm-text-muted)", flexShrink: 0 }} />
    </button>
  );
}

// ─── Detail view ─────────────────────────────────────────────────────────────

function SkillDetail({
  skill, onBack, onClose,
}: {
  skill: InstalledSkill;
  onBack: () => void;
  onClose: () => void;
}) {
  const [files, setFiles] = useState<SkillBundleFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    readSkillFiles(skill.name)
      .then((b) => { if (alive) setFiles(b.files ?? []); })
      .catch((e) => { if (alive) setError(e instanceof Error ? e.message : "Failed to read the skill"); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [skill.name]);

  return (
    <ModalShell
      icon={FileText}
      title={`/${skill.name}`}
      subtitle={skill.description || "Installed on this runtime"}
      width={940}
      onClose={onClose}
      onBack={onBack}
      bodyPadding={0}
    >
      {loading ? (
        <Filler><Loader2 size={16} className="lm-spin-ico" /> Reading files…</Filler>
      ) : error ? (
        <Filler tone="error"><AlertCircle size={16} /> {error}</Filler>
      ) : (
        <SkillFilesView files={files} />
      )}
    </ModalShell>
  );
}

// ─── Bits ────────────────────────────────────────────────────────────────────

// countFiles walks the tree the listing returns (dirs carry children) so the
// card can show a size without a second request.
function countFiles(skill: InstalledSkill): number {
  let n = 0;
  const walk = (nodes: InstalledSkill["files"]) => {
    for (const node of nodes) {
      if (node.dir) walk(node.children ?? []);
      else n++;
    }
  };
  walk(skill.files ?? []);
  return n;
}

function Centered({ children, tone }: { children: React.ReactNode; tone?: "error" }) {
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
      padding: "44px 12px", textAlign: "center",
      fontSize: 12.5, color: tone === "error" ? "var(--lm-red)" : "var(--lm-text-muted)",
    }}>{children}</div>
  );
}

// Filler fills the full-bleed body of the detail shell (bodyPadding={0}).
function Filler({ children, tone }: { children: React.ReactNode; tone?: "error" }) {
  return (
    <div style={{
      flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
      fontSize: 12.5, color: tone === "error" ? "var(--lm-red)" : "var(--lm-text-muted)",
    }}>{children}</div>
  );
}
