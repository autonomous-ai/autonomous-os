import { useCallback, useState } from "react";
import { hwUrl } from "@/lib/api";

export interface FaceOwner {
  label: string;
  photo_count: number;
  photos: string[];
  voice_samples?: string[];
}

// Enrolled-owners list for Setup's continue-mode Voice/Face steps. The enroll,
// upload, and per-owner/photo removal flows now live inside the shared Settings
// components (pages/settings/VoiceSection + FaceSection), so this hook only
// exposes the owners list and a reload fn — Setup passes both down and uses the
// list to drive sectionDone (voice/face). Uses the token-aware hwUrl so it works
// the same way the Settings page does.
export function useFaceEnroll() {
  const [faceOwners, setFaceOwners] = useState<FaceOwner[]>([]);

  const loadFaceOwners = useCallback(async () => {
    try {
      const r = await fetch(hwUrl("/face/owners")).then((x) => x.json());
      if (Array.isArray(r?.persons)) setFaceOwners(r.persons);
    } catch { /* hardware unreachable in initial mode; silent */ }
  }, []);

  return { faceOwners, loadFaceOwners };
}
