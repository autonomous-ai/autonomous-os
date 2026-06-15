import { useEffect } from "react";

const BASE = "Autonomous";

export function useDocumentTitle(parts: string | string[]) {
  useEffect(() => {
    const segs = (Array.isArray(parts) ? parts : [parts]).filter(Boolean);
    const next = segs.length ? `${BASE} · ${segs.join(" · ")}` : BASE;
    const prev = document.title;
    document.title = next;
    return () => {
      document.title = prev;
    };
  }, [Array.isArray(parts) ? parts.join("|") : parts]);
}
