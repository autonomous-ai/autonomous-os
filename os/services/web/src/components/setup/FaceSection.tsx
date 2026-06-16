import type { RefObject } from "react";
import { UserCircle } from "lucide-react";
import { C, Field, SectionCard } from "./shared";
import type { FaceOwner } from "@/hooks/setup/useFaceEnroll";

export function FaceSection({
  active,
  faceName, setFaceName,
  faceFiles, setFaceFiles,
  faceUploading,
  faceMsg,
  faceInputRef,
  faceOwners,
  removeFaceOwner,
  handleFaceEnroll,
}: {
  active: boolean;
  faceName: string; setFaceName: (v: string) => void;
  faceFiles: File[]; setFaceFiles: (files: File[]) => void;
  faceUploading: boolean;
  faceMsg: string | null;
  faceInputRef: RefObject<HTMLInputElement | null>;
  faceOwners: FaceOwner[];
  removeFaceOwner: (label: string) => void;
  handleFaceEnroll: () => void;
}) {
  return (
    <SectionCard id="face" title="Face Enroll (optional)" active={active} icon={<UserCircle size={17} />}
      description="Upload a few photos so your device can recognize you on sight.">
      <Field label="Name" id="face_name" value={faceName} onChange={setFaceName} placeholder="e.g. Leo" />
      <div style={{ marginBottom: 12 }}>
        <input
          ref={faceInputRef} type="file" accept="image/*" multiple
          onChange={(e) => setFaceFiles(Array.from(e.target.files ?? []))}
          style={{ fontSize: 12, color: C.text }}
        />
      </div>
      <button
        type="button"
        disabled={faceUploading || !faceName.trim() || faceFiles.length === 0}
        onClick={handleFaceEnroll}
        style={{
          width: "100%", padding: "10px 0",
          background: !faceUploading && faceName.trim() && faceFiles.length > 0 ? C.amber : C.surface,
          color: !faceUploading && faceName.trim() && faceFiles.length > 0 ? "#0C0B09" : C.textDim,
          border: "none", borderRadius: 8, fontSize: 13,
          cursor: faceUploading ? "default" : "pointer", fontWeight: 600,
        }}
      >
        {faceUploading ? "Uploading..." : "Upload"}
      </button>
      {faceMsg && (
        <div style={{ fontSize: 11, color: faceMsg.startsWith("Error") ? C.red : C.green, marginTop: 8 }}>
          {faceMsg}
        </div>
      )}
      {faceOwners.length > 0 && (
        <div style={{ marginTop: 14, paddingTop: 12, borderTop: `1px solid ${C.border}` }}>
          <div style={{ fontSize: 11, color: C.textDim, marginBottom: 8 }}>Enrolled:</div>
          {faceOwners.map((p) => (
            <div key={p.label} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "4px 0", fontSize: 12 }}>
              <span>{p.label} <span style={{ color: C.textMuted }}>({p.photo_count} photos)</span></span>
              <button type="button" onClick={() => removeFaceOwner(p.label)} style={{
                background: "none", border: "none", color: C.red, cursor: "pointer", fontSize: 11,
              }}>remove</button>
            </div>
          ))}
        </div>
      )}
    </SectionCard>
  );
}
