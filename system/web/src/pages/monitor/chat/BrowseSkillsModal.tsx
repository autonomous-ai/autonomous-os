import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Store, Search, Loader2, RefreshCw, AlertCircle, FileText, ChevronRight, Download, Check,
} from "lucide-react";
import { browseStoreSkills, fetchSkillBundle, installStoreSkill } from "@/lib/api";
import type { StoreSkill, SkillBundle } from "@/lib/api";
import { ModalShell } from "./ModalShell";
import { SkillFilesView } from "./SkillFilesView";
import { inputStyle, btnStyle } from "./styles";

// "Browse skills" — the Autonomous Agent Skills catalog, two views in one modal:
//
//   list   → GET /api/agent/skills/browse   (catalog listing, keyword-searchable)
//   detail → GET /api/agent/skills/bundle   (device downloads the .skill archive
//            to a temp dir, unzips it, and returns the files with text inlined)
//
// Both hops go through os-server, never the browser: no CORS round-trip, and
// the catalog host stays server-side. Opening a skill is a PREVIEW — the temp
// dir is deleted server-side once the response is built, nothing is installed.

const PAGE_LIMIT = 50;

export function BrowseSkillsModal({ onClose }: { onClose: () => void }) {
  const [selected, setSelected] = useState<StoreSkill | null>(null);
  return selected
    // key: a detail view is bound to one skill for its whole lifetime, so its
    // fetch effect never has to reset state for a different id.
    ? <SkillDetail key={selected.id} skill={selected} onBack={() => setSelected(null)} onClose={onClose} />
    : <SkillList onOpen={setSelected} onClose={onClose} />;
}

// ─── List view ───────────────────────────────────────────────────────────────

function SkillList({
  onOpen, onClose,
}: {
  onOpen: (s: StoreSkill) => void;
  onClose: () => void;
}) {
  const [skills, setSkills] = useState<StoreSkill[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  // Debounced copy of `query` — the catalog does the keyword match, so every
  // keystroke would otherwise be a request.
  const [keyword, setKeyword] = useState("");

  useEffect(() => {
    const t = setTimeout(() => setKeyword(query.trim()), 300);
    return () => clearTimeout(t);
  }, [query]);

  const load = useCallback(async (kw: string) => {
    setLoading(true);
    setError("");
    try {
      const res = await browseStoreSkills({ keyword: kw || undefined, page: 1, limit: PAGE_LIMIT });
      setSkills(res.data ?? []);
      setTotal(res.total ?? 0);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to reach the skill store");
      setSkills([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(keyword); }, [load, keyword]);

  const subtitle = useMemo(() => {
    if (loading || error) return "Autonomous skill store";
    const shown = skills.length;
    return total > shown ? `${shown} of ${total} skills` : `${shown} skill${shown === 1 ? "" : "s"}`;
  }, [loading, error, skills.length, total]);

  return (
    <ModalShell icon={Store} title="Browse skills" subtitle={subtitle} width={640} onClose={onClose}>
      <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
        <div style={{ position: "relative", flex: 1 }}>
          <Search size={14} style={{
            position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)",
            color: "var(--lm-text-muted)", pointerEvents: "none",
          }} />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search skills…"
            className="lm-u-input"
            style={{ ...inputStyle, paddingLeft: 30 }}
          />
        </div>
        <button
          type="button"
          className="lm-u-btn"
          onClick={() => void load(keyword)}
          disabled={loading}
          title="Reload"
          aria-label="Reload"
          style={{
            width: 36, borderRadius: 9, display: "flex",
            alignItems: "center", justifyContent: "center", flexShrink: 0,
          }}
        ><RefreshCw size={14} className={loading ? "lm-spin-ico" : undefined} /></button>
      </div>

      {loading ? (
        <Centered><Loader2 size={18} className="lm-spin-ico" /> Loading skills…</Centered>
      ) : error ? (
        <Centered tone="error"><AlertCircle size={18} /> {error}</Centered>
      ) : skills.length === 0 ? (
        <Centered>{keyword ? `No skill matches “${keyword}”.` : "The store returned no skills."}</Centered>
      ) : (
        // Two per row (auto-fit collapses to one on a narrow viewport) — a
        // one-per-row list wastes the modal's width on short skill names.
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))",
          gap: 8,
        }}>
          {skills.map((s) => <StoreRow key={s.id} skill={s} onOpen={() => onOpen(s)} />)}
        </div>
      )}
    </ModalShell>
  );
}

function StoreRow({ skill, onOpen }: { skill: StoreSkill; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      style={{
        display: "flex", alignItems: "center", gap: 10, width: "100%", textAlign: "left",
        padding: "10px 12px", borderRadius: 10, cursor: "pointer",
        background: "var(--lm-card)", border: "1px solid var(--lm-border)",
        color: "var(--lm-text)", transition: "background 0.12s, border-color 0.12s",
      }}
      onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--lm-border-hi)"; }}
      onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--lm-border)"; }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>{skill.name}</span>
          {skill.version && (
            <span style={{ fontSize: 10, color: "var(--lm-text-muted)", fontFamily: "monospace" }}>v{skill.version}</span>
          )}
          {skill.plan_required && skill.plan_required !== "free" && (
            <Chip tone="amber">{skill.plan_required.toUpperCase()}</Chip>
          )}
        </div>
        {skill.description && (
          <div style={{
            fontSize: 11.5, color: "var(--lm-text-dim)", marginTop: 3, lineHeight: 1.45,
            display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden",
          }}>{skill.description}</div>
        )}
        <div style={{ display: "flex", gap: 6, marginTop: 6, flexWrap: "wrap", alignItems: "center" }}>
          {skill.author && <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>by {skill.author}</span>}
          {(skill.compatibility ?? []).map((t) => <Chip key={t}>{t}</Chip>)}
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
  skill: StoreSkill;
  onBack: () => void;
  onClose: () => void;
}) {
  const [bundle, setBundle] = useState<SkillBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // Install state — separate from the preview fetch above so a failed install
  // doesn't wipe the file browser the user is reading.
  const [installing, setInstalling] = useState(false);
  const [installed, setInstalled] = useState("");
  const [installError, setInstallError] = useState("");

  const install = async () => {
    setInstalling(true);
    setInstallError("");
    try {
      const res = await installStoreSkill(skill.id, skill.slug || skill.name);
      setInstalled(res.name);
    } catch (e) {
      setInstallError(e instanceof Error ? e.message : "Failed to install the skill");
    } finally {
      setInstalling(false);
    }
  };

  useEffect(() => {
    let alive = true;
    fetchSkillBundle(skill.id)
      .then((b) => {
        if (!alive) return;
        setBundle(b);
      })
      .catch((e) => { if (alive) setError(e instanceof Error ? e.message : "Failed to download the skill"); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [skill.id]);

  return (
    <ModalShell
      icon={FileText}
      title={skill.name}
      subtitle={[skill.version && `v${skill.version}`, skill.author && `by ${skill.author}`]
        .filter(Boolean).join(" · ") || undefined}
      width={940}
      onClose={onClose}
      onBack={onBack}
      bodyPadding={0}
      footer={
        <div style={{ display: "flex", alignItems: "center", gap: 12, width: "100%" }}>
          <div style={{ flex: 1, minWidth: 0, fontSize: 11, lineHeight: 1.45 }}>
            {installError ? (
              <span style={{ color: "var(--lm-red)" }}>{installError}</span>
            ) : installed ? (
              <span style={{ color: "var(--lm-text-dim)" }}>
                Installed as <strong>/{installed}</strong> — the agent picks it up on its next session.
              </span>
            ) : (
              <span style={{ color: "var(--lm-text-muted)" }}>
                Installs into the active runtime's skills dir. Replaces an existing skill of the same name.
              </span>
            )}
          </div>
          <button
            type="button"
            className="lm-u-btn lm-u-btn-primary"
            style={{
              ...btnStyle, flexShrink: 0,
              display: "inline-flex", alignItems: "center", gap: 6,
              opacity: installing || installed || loading || Boolean(error) ? 0.55 : 1,
              cursor: installing || installed || loading || Boolean(error) ? "not-allowed" : "pointer",
            }}
            disabled={installing || Boolean(installed) || loading || Boolean(error)}
            onClick={() => void install()}
          >
            {installed
              ? <><Check size={14} /> Installed</>
              : installing
                ? <><Loader2 size={14} className="lm-spin-ico" /> Installing…</>
                : <><Download size={14} /> Install</>}
          </button>
        </div>
      }
    >
      {loading ? (
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 8, fontSize: 12.5, color: "var(--lm-text-muted)" }}>
          <Download size={16} className="lm-spin-ico" /> Downloading and unpacking…
        </div>
      ) : error ? (
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 8, fontSize: 12.5, color: "var(--lm-red)" }}>
          <AlertCircle size={16} /> {error}
        </div>
      ) : (
        <SkillFilesView files={bundle?.files ?? []} skipped={bundle?.skipped} />
      )}
    </ModalShell>
  );
}

// ─── Bits ────────────────────────────────────────────────────────────────────

function Chip({ children, tone }: { children: React.ReactNode; tone?: "amber" }) {
  return (
    <span style={{
      fontSize: 9.5, padding: "2px 6px", borderRadius: 999,
      color: tone === "amber" ? "var(--lm-amber)" : "var(--lm-text-muted)",
      background: tone === "amber" ? "color-mix(in srgb, var(--lm-amber) 12%, transparent)" : "var(--lm-bg)",
      border: `1px solid ${tone === "amber" ? "transparent" : "var(--lm-border)"}`,
      fontWeight: tone === "amber" ? 600 : 400,
    }}>{children}</span>
  );
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
