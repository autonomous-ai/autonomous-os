import { useCallback, useRef, useState } from "react";

export interface FaceOwner {
  label: string;
  photo_count: number;
  photos: string[];
  voice_samples?: string[];
}

// State + handlers for the Face enroll flow (continue mode). Same endpoints
// as EditConfig.Face; kept here so Setup can show enrolled owners.
// Voice enrollment lives separately in Setup.tsx for now — it reloads
// `loadFaceOwners` after each enroll so voice samples surface in this list.
export function useFaceEnroll() {
  const [faceName, setFaceName] = useState("");
  const [faceFiles, setFaceFiles] = useState<File[]>([]);
  const [faceUploading, setFaceUploading] = useState(false);
  const [faceMsg, setFaceMsg] = useState<string | null>(null);
  const faceInputRef = useRef<HTMLInputElement>(null);
  const [faceOwners, setFaceOwners] = useState<FaceOwner[]>([]);

  const loadFaceOwners = useCallback(async () => {
    try {
      const r = await fetch("/api/hardware/face/owners").then((x) => x.json());
      if (Array.isArray(r?.persons)) setFaceOwners(r.persons);
    } catch { /* hardware unreachable in initial mode; silent */ }
  }, []);

  const removeFaceOwner = useCallback(async (label: string) => {
    if (!confirm(`Remove enrolled face "${label}"?`)) return;
    try {
      await fetch("/api/hardware/face/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label }),
      });
      loadFaceOwners();
    } catch { /* ignore */ }
  }, [loadFaceOwners]);

  const handleFaceEnroll = useCallback(async () => {
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
        const resp = await fetch("/api/hardware/face/enroll", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label, image_base64: b64 }),
        });
        const data = await resp.json();
        if (resp.ok) ok++;
        else lastErr = data.detail || data.message || `Failed: ${file.name}`;
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
  }, [faceName, faceFiles, loadFaceOwners]);

  return {
    faceName, setFaceName,
    faceFiles, setFaceFiles,
    faceUploading,
    faceMsg, setFaceMsg,
    faceInputRef,
    faceOwners,
    loadFaceOwners,
    removeFaceOwner,
    handleFaceEnroll,
  };
}
