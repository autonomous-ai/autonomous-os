import { useRef, useState } from "react";
import { UserCircle, UserPlus, ImagePlus, Loader2 } from "lucide-react";
import { hwUrl } from "@/lib/api";
import { C, Field, SectionCard, LABEL_STYLE } from "@/components/setup/shared";
import type { FaceOwner } from "@/hooks/setup/useFaceEnroll";

// Face enroll for edit-mode owners. State is local since nothing outside the
// section reads it; faceOwners list comes from the page so Voice section can
// share it.
export function FaceSection({
  active, faceOwners, loadFaceOwners,
}: {
  active: boolean;
  faceOwners: FaceOwner[];
  loadFaceOwners: () => Promise<void>;
}) {
  const [faceName, setFaceName] = useState("");
  const [faceFiles, setFaceFiles] = useState<File[]>([]);
  const [faceUploading, setFaceUploading] = useState(false);
  const [faceMsg, setFaceMsg] = useState<string | null>(null);
  const faceInputRef = useRef<HTMLInputElement>(null);
  const [faceExpanded, setFaceExpanded] = useState<Record<string, boolean>>({});
  const toggleFaceExpanded = (label: string) =>
    setFaceExpanded((prev) => ({ ...prev, [label]: !prev[label] }));

  const removeFaceOwner = async (label: string) => {
    if (!confirm(`Remove "${label}"?`)) return;
    try {
      await fetch("/hw/face/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label }),
      });
      loadFaceOwners();
    } catch {}
  };

  const removeFacePhoto = async (label: string, filename: string) => {
    if (!confirm(`Delete photo "${filename}" for "${label}"?`)) return;
    try {
      await fetch("/hw/face/photo/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label, filename }),
      });
      loadFaceOwners();
    } catch { /* ignore */ }
  };

  const handleFaceEnroll = async () => {
    if (!faceName.trim() || faceFiles.length === 0) return;
    setFaceUploading(true);
    setFaceMsg(null);
    const label = faceName.trim().toLowerCase();
    let ok = 0;
    let lastErr = "";
    for (const file of faceFiles) {
      try {
        const buf = await file.arrayBuffer();
        const b64 = btoa(String.fromCharCode(...new Uint8Array(buf)));
        const resp = await fetch("/hw/face/enroll", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label, image_base64: b64 }),
        });
        const data = await resp.json();
        if (resp.ok) {
          ok++;
        } else {
          lastErr = data.detail || data.message || `Failed: ${file.name}`;
        }
      } catch (e) {
        lastErr = e instanceof Error ? e.message : String(e);
      }
    }
    if (ok > 0) {
      setFaceMsg(`Enrolled "${label}" — ${ok}/${faceFiles.length} photos`
        + (lastErr ? ` (${lastErr})` : ""));
      setFaceName("");
      setFaceFiles([]);
      if (faceInputRef.current) faceInputRef.current.value = "";
      loadFaceOwners();
    } else {
      setFaceMsg(`Error: ${lastErr}`);
    }
    setFaceUploading(false);
  };

  const enrolled = faceOwners.filter((p) => p.photo_count > 0);

  return (
    <SectionCard
      id="face"
      title="Face Enroll (optional)"
      active={active}
      description="Upload photos of the owner so your device can recognize them."
      icon={<UserCircle size={17} />}
    >
      <Field label="Name" id="face_name" value={faceName} onChange={setFaceName} placeholder="e.g. Leo" />
      <div style={{ marginBottom: 14 }}>
        <label style={{ ...LABEL_STYLE, marginBottom: 8 }}>Photos</label>
        {/* Hidden native input keeps all enroll logic intact; the styled
            dropzone below is just a click target that proxies to it. */}
        <input
          ref={faceInputRef}
          type="file"
          accept="image/*"
          multiple
          onChange={(e) => setFaceFiles(e.target.files ? Array.from(e.target.files) : [])}
          style={{ display: "none" }}
        />
        <button
          type="button"
          onClick={() => faceInputRef.current?.click()}
          className="lm-face-drop"
          style={{
            width: "100%", boxSizing: "border-box", cursor: "pointer",
            display: "flex", alignItems: "center", gap: 12,
            padding: "14px 16px", borderRadius: 12,
            border: `1px dashed ${faceFiles.length ? "var(--lm-amber-glow)" : C.border}`,
            background: faceFiles.length ? "var(--lm-amber-dim)" : C.surface,
            color: faceFiles.length ? C.amber : C.textDim,
            transition: "all 0.15s",
          }}
        >
          <ImagePlus size={20} style={{ flexShrink: 0 }} />
          <span style={{ flex: 1, textAlign: "left", fontSize: 13, fontWeight: faceFiles.length ? 600 : 400 }}>
            {faceFiles.length
              ? `${faceFiles.length} photo${faceFiles.length !== 1 ? "s" : ""} selected`
              : "Choose photos…"}
          </span>
          <span style={{ flexShrink: 0, fontSize: 11, color: C.textMuted }}>
            {faceFiles.length ? "Change" : "Browse"}
          </span>
        </button>
      </div>
      {faceMsg && (
        <div style={{
          fontSize: 11, padding: "6px 10px", borderRadius: 8, marginBottom: 10,
          background: faceMsg.startsWith("Error") || faceMsg.includes("failed")
            ? "var(--lm-red-dim)" : "var(--lm-green-dim)",
          color: faceMsg.startsWith("Error") || faceMsg.includes("failed")
            ? C.red : C.green,
        }}>{faceMsg}</div>
      )}
      <button
        type="button"
        onClick={handleFaceEnroll}
        disabled={!faceName.trim() || faceFiles.length === 0 || faceUploading}
        style={{
          width: "100%", padding: "10px 0", borderRadius: 10, fontSize: 12.5,
          fontWeight: 600, cursor: faceUploading ? "wait" : "pointer",
          transition: "all 0.15s",
          display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
          background: !faceName.trim() || faceFiles.length === 0 ? C.surface : "var(--lm-green-dim)",
          border: `1px solid ${!faceName.trim() || faceFiles.length === 0 ? C.border : "var(--lm-green-glow)"}`,
          color: !faceName.trim() || faceFiles.length === 0 ? C.textMuted : C.green,
        }}
      >
        {faceUploading
          ? <><Loader2 size={15} className="lm-spin-ico" />Uploading…</>
          : <><UserPlus size={15} />Enroll Face</>}
      </button>
      {enrolled.length > 0 && (
        <div style={{ marginTop: 16, borderTop: `1px solid ${C.border}`, paddingTop: 14 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.textDim, textTransform: "uppercase", letterSpacing: "0.09em", marginBottom: 10 }}>
            Face Photos
          </div>
          {enrolled.map((p) => {
            const expanded = !!faceExpanded[p.label];
            return (
              <div key={p.label} style={{ padding: "10px 0", borderBottom: `1px solid ${C.border}` }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: expanded ? 8 : 0 }}>
                  <button
                    type="button"
                    onClick={() => toggleFaceExpanded(p.label)}
                    style={{
                      flex: 1, display: "flex", alignItems: "center", gap: 8,
                      background: "none", border: "none", cursor: "pointer", padding: 0,
                      textAlign: "left", color: C.text,
                    }}
                  >
                    <span style={{ fontSize: 11, color: C.textMuted, transition: "transform 0.15s", transform: expanded ? "rotate(90deg)" : "none" }}>▶</span>
                    <span style={{ fontSize: 13, fontWeight: 600 }}>{p.label}</span>
                    <span style={{ fontSize: 10, color: C.textMuted, fontWeight: 400 }}>({p.photo_count} photo{p.photo_count !== 1 ? "s" : ""})</span>
                  </button>
                  {p.label !== "unknown" && (
                    <button
                      type="button"
                      onClick={() => removeFaceOwner(p.label)}
                      style={{
                        background: "none", border: `1px solid ${C.border}`, borderRadius: 5,
                        cursor: "pointer", fontSize: 10, color: C.red, padding: "3px 8px",
                      }}
                    >
                      Remove all
                    </button>
                  )}
                </div>
                {expanded && p.photos.length > 0 && (
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {p.photos.map((photo) => (
                      <div key={photo} style={{ position: "relative", width: 56, height: 56 }}>
                        <img
                          src={hwUrl(`/face/photo/${encodeURIComponent(p.label)}/${encodeURIComponent(photo)}`)}
                          title={photo}
                          onClick={() => window.open(hwUrl(`/face/photo/${encodeURIComponent(p.label)}/${encodeURIComponent(photo)}`), "_blank", "noopener,noreferrer")}
                          style={{
                            width: 56, height: 56, borderRadius: 8, objectFit: "cover",
                            border: `1px solid ${C.border}`, cursor: "pointer", display: "block",
                          }}
                        />
                        {p.label !== "unknown" && (
                          <button
                            type="button"
                            onClick={(e) => { e.stopPropagation(); removeFacePhoto(p.label, photo); }}
                            title={`Delete ${photo}`}
                            style={{
                              position: "absolute", top: -6, right: -6,
                              width: 18, height: 18, borderRadius: "50%",
                              background: C.bg, border: `1px solid ${C.border}`,
                              cursor: "pointer", fontSize: 11, lineHeight: "16px",
                              color: C.red, padding: 0, textAlign: "center",
                            }}
                          >
                            ×
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </SectionCard>
  );
}
