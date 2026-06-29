import { UserRound } from "lucide-react";
import { hwUrl } from "@/lib/api";

// Small circular avatar for a recognized user: shows the enrolled face photo
// when one is known, else a generic Lucide UserRound glyph. The <img> falls
// back to the icon on load error so a deleted/renamed photo never shows broken.
// Used by the Flow Panel header chip and the per-turn badges so "who" reads
// identically everywhere. `photo` is the first-enrolled filename for `user`
// (mapped from GET /face/owners); pass undefined for unknown/strangers.
export function UserAvatar({ user, photo, size = 18, color }: {
  user: string;
  photo?: string;
  size?: number;
  color: string;
}) {
  const iconSize = Math.round(size * 0.62);
  return (
    <span
      aria-hidden
      style={{
        width: size, height: size, borderRadius: "50%", flexShrink: 0,
        overflow: "hidden", display: "inline-flex", alignItems: "center", justifyContent: "center",
        background: `${color}22`, color,
      }}
    >
      {photo ? (
        <img
          src={hwUrl(`/face/photo/${encodeURIComponent(user)}/${encodeURIComponent(photo)}`)}
          alt=""
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
          onError={(e) => {
            // Hide the broken <img>; the sibling icon then shows.
            const img = e.currentTarget as HTMLImageElement;
            img.style.display = "none";
            const sib = img.nextElementSibling as HTMLElement | null;
            if (sib) sib.style.display = "inline-flex";
          }}
        />
      ) : null}
      <UserRound
        size={iconSize}
        strokeWidth={2.25}
        style={{ display: photo ? "none" : "inline-flex" }}
      />
    </span>
  );
}
