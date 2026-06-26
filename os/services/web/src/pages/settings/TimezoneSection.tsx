import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { toast } from "sonner";
import { C, SectionCard, LABEL_STYLE, INPUT_STYLE } from "@/components/setup/shared";
import { getTimezone, setTimezone } from "@/lib/api";
import { useTheme } from "@/lib/useTheme";

// Timezone picker. Like the phone "Date & Time" setting: pick an IANA zone and
// apply. Not part of the form's "Save Changes" flow — it has its own Apply
// button hitting POST /api/device/timezone directly (writes /etc/localtime +
// /etc/timezone). The zone list comes from the device (system tzdata via
// timedatectl), so the UI never hardcodes it.
//
// The list is long (the full IANA set, ~550 zones), so the field is a button
// showing the current selection; clicking it opens a centered MODAL with a
// search box on top and a region-grouped, filtered list below. Type to narrow
// ("ho chi", "+7", "london"), arrow/enter to pick, escape/click-outside/✕ to
// close. The modal is portaled to <body> so it overlays the whole app and never
// reflows the settings card (the old in-card absolute popover caused jank).


// formatZoneTime renders the current wall-clock in `zone` as a friendly preview,
// so the operator sees what local time the selection implies (iPhone-style).
// Returns "" when the zone is invalid / not yet resolvable.
function formatZoneTime(zone: string): string {
  if (!zone) return "";
  try {
    return new Intl.DateTimeFormat(undefined, {
      timeZone: zone, weekday: "short", hour: "2-digit", minute: "2-digit",
      hour12: false,
    }).format(new Date());
  } catch {
    return "";
  }
}

// regionOf is the optgroup bucket for a zone: the part before the first "/"
// (Asia, Europe, America…), or "Other" for flat zones like UTC.
function regionOf(zone: string): string {
  const i = zone.indexOf("/");
  return i === -1 ? "Other" : zone.slice(0, i);
}

// cityOf is the friendly location part with underscores turned into spaces
// (e.g. "Asia/Ho_Chi_Minh" → "Ho Chi Minh"). Flat zones show as-is.
function cityOf(zone: string): string {
  const i = zone.indexOf("/");
  const tail = i === -1 ? zone : zone.slice(i + 1);
  return tail.replace(/_/g, " ");
}

// gmtOffset returns the zone's current UTC offset as a short string like
// "GMT+7" / "GMT+5:30" / "GMT-8" (DST-aware), or "" when it can't resolve.
// Shown in each option the way WordPress / Google / AWS timezone pickers do.
function gmtOffset(zone: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: zone, timeZoneName: "shortOffset",
    }).formatToParts(new Date());
    return parts.find((p) => p.type === "timeZoneName")?.value ?? "";
  } catch {
    return "";
  }
}

// offsetMinutes is the signed UTC offset in minutes, used to sort zones within a
// region by offset (the order most web pickers use). Unresolvable → large so it
// sinks to the bottom.
function offsetMinutes(zone: string): number {
  const o = gmtOffset(zone).replace("GMT", "").trim(); // "+7", "+5:30", "-8", ""
  if (!o) return 9999;
  const m = /^([+-])(\d{1,2})(?::(\d{2}))?$/.exec(o);
  if (!m) return 0;
  const sign = m[1] === "-" ? -1 : 1;
  return sign * (parseInt(m[2], 10) * 60 + (m[3] ? parseInt(m[3], 10) : 0));
}

// labelOf is the full option text shown in the list: "(GMT+7) Ho Chi Minh".
function labelOf(zone: string): string {
  const off = gmtOffset(zone);
  const city = cityOf(zone);
  return off ? `(${off}) ${city}` : city;
}

type ZoneOpt = { value: string; label: string; off: number; region: string };

export function TimezoneSection({ active }: { active: boolean }) {
  // The picker modal is portaled to <body>, outside the Monitor shell's
  // `.lm-root`, so it needs its own theme class to keep the --lm-* tokens (and
  // light/dark variant) in scope — same `lm-root ${themeClass}` contract the
  // Monitor/Setup/Login pages use.
  const [, , themeClass] = useTheme();
  const [current, setCurrent] = useState<string>("");
  const [zones, setZones] = useState<string[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  // Tick once a minute so the live-time preview stays current while the section
  // is open. Cheap; only the preview string depends on it.
  const [, setTick] = useState(0);

  // Modal state: whether the picker dialog is open, the search query, and which
  // visible row is highlighted (for keyboard nav).
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getTimezone()
      .then((r) => {
        setCurrent(r.current);
        setSelected(r.current);
        if (r.zones?.length) setZones(r.zones);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 60_000);
    return () => clearInterval(id);
  }, []);

  // All zones as flat options, each carrying a precomputed "(GMT+x) City" label
  // and its region/offset for grouping + sorting. Built ONCE per zone list — the
  // Intl.DateTimeFormat calls (two per zone × ~550 zones) are the expensive bit,
  // so we keep them out of the per-keystroke filter path below.
  const options = useMemo<ZoneOpt[]>(
    () =>
      zones.map((z) => ({
        value: z, label: labelOf(z), off: offsetMinutes(z), region: regionOf(z),
      })),
    [zones],
  );

  // Filtered + region-grouped options for the open modal. The query matches
  // against the raw zone name, the friendly label, and the GMT offset, so
  // "ho chi", "asia/ho", "(gmt+7)" and "+7" all find Ho Chi Minh. Within a
  // region, ordered by UTC offset then name (common web-picker order). Runs on
  // the precomputed `options` only — no Intl calls here, so typing stays smooth.
  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matched = q
      ? options.filter(
          (o) =>
            o.value.toLowerCase().includes(q) ||
            o.label.toLowerCase().includes(q),
        )
      : options;
    const byRegion = new Map<string, ZoneOpt[]>();
    for (const o of matched) {
      (byRegion.get(o.region) ?? byRegion.set(o.region, []).get(o.region)!).push(o);
    }
    for (const list of byRegion.values()) {
      list.sort((a, b) => a.off - b.off || a.label.localeCompare(b.label));
    }
    return [...byRegion.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [options, query]);

  // Flat list of visible options (group order), so keyboard highlight maps to a
  // single index regardless of grouping.
  const flat = useMemo(() => groups.flatMap(([, list]) => list), [groups]);

  const preview = useMemo(() => formatZoneTime(selected), [selected]);
  const selectedLabel = selected ? labelOf(selected) : "";

  // When the modal opens: reset search, point the highlight at the currently
  // selected zone (or the top), focus the search box, and lock body scroll so
  // the page behind doesn't move. Esc closes from anywhere in the dialog.
  useEffect(() => {
    if (!open) return;
    setQuery("");
    const i = flat.findIndex((o) => o.value === selected);
    setHighlight(i >= 0 ? i : 0);
    const t = setTimeout(() => searchRef.current?.focus(), 0);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", onKey);
    return () => {
      clearTimeout(t);
      document.body.style.overflow = prevOverflow;
      document.removeEventListener("keydown", onKey);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Keep the highlighted row in view as the user arrows through.
  useEffect(() => {
    if (!open) return;
    listRef.current
      ?.querySelector<HTMLElement>(`[data-idx="${highlight}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [highlight, open]);

  // Picking a row selects it, closes the modal, AND applies immediately — saves
  // the operator the extra "Apply" click. The standalone Apply button below
  // stays as a fallback (e.g. re-applying the already-selected zone). We apply
  // by the passed `value`, not the `selected` state, because setSelected is
  // async and wouldn't be visible to applyZone yet.
  function pick(value: string) {
    setSelected(value);
    setOpen(false);
    applyZone(value);
  }

  function onSearchKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => Math.min(h + 1, flat.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const o = flat[highlight];
      if (o) pick(o.value);
    }
    // Escape is handled by the dialog-level keydown listener.
  }

  // applyZone POSTs the given IANA zone. Takes the zone as an argument (rather
  // than reading `selected`) so it works straight from pick() before the
  // setSelected state has flushed. No-ops on empty / already-active / in-flight
  // / invalid zones, matching the Apply button's disabled conditions.
  async function applyZone(zone: string) {
    if (!zone || zone === current || applying || !zones.includes(zone)) return;
    setApplying(true);
    try {
      await setTimezone(zone);
      setCurrent(zone);
      toast.success(`Timezone set to ${zone}.`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to set timezone.");
    } finally {
      setApplying(false);
    }
  }

  let flatIdx = 0; // running index across groups, to align rows with `flat`

  // The picker modal, portaled to <body> so it overlays the whole app (no
  // reflow of the settings card). Backdrop click + ✕ + Esc all close it.
  const modal =
    open &&
    createPortal(
      // Portaled to <body>, OUTSIDE the Monitor shell's `.lm-root`. The `C.*`
      // tokens resolve to `var(--lm-*)`, which are only defined under `.lm-root`
      // — so without this className the panel renders transparent. Re-scoping
      // `.lm-root` here brings the theme tokens (dark/light) back in scope.
      <div
        className={`lm-root ${themeClass}`}
        onClick={() => setOpen(false)}
        style={{
          position: "fixed", inset: 0, zIndex: 1000,
          // Override .lm-root's opaque --lm-bg fill with a translucent scrim so
          // the page stays visible behind the dialog.
          background: "rgba(0,0,0,0.66)", backdropFilter: "blur(3px)",
          display: "flex", alignItems: "center", justifyContent: "center",
          padding: 16,
        }}
      >
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Select timezone"
          onClick={(e) => e.stopPropagation()}
          style={{
            // FIXED height (not max-height) so the modal never resizes when the
            // result count changes — searching down to few/no matches keeps the
            // same box, no jank. `min(...)` keeps it responsive: capped at 560px
            // on desktop, 86vh on short/mobile viewports.
            width: "100%", maxWidth: 520, height: "min(560px, 86vh)",
            background: C.card, border: `1px solid ${C.border}`,
            borderRadius: 12, boxShadow: "0 24px 64px rgba(0,0,0,0.55)",
            display: "flex", flexDirection: "column", overflow: "hidden",
          }}
        >
          {/* Header: title + close. */}
          <div
            style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              padding: "13px 15px", borderBottom: `1px solid ${C.border}`, flexShrink: 0,
            }}
          >
            <span style={{ fontSize: 14, fontWeight: 600, color: C.text }}>
              Select timezone
            </span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close"
              style={{
                width: 32, height: 32, borderRadius: 8, padding: 0,
                display: "flex", alignItems: "center", justifyContent: "center",
                background: C.surface, border: `1px solid ${C.border}`,
                color: C.textDim, cursor: "pointer", fontSize: 15,
              }}
            >
              ✕
            </button>
          </div>

          {/* Search box — uses the shared INPUT_STYLE (14px) so it matches the
              text fields across the rest of Settings. */}
          <div style={{ padding: 12, borderBottom: `1px solid ${C.border}`, flexShrink: 0 }}>
            <input
              ref={searchRef}
              type="text"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setHighlight(0);
              }}
              onKeyDown={onSearchKeyDown}
              placeholder="Search city, region or GMT offset…"
              style={INPUT_STYLE}
            />
          </div>

          {/* Scrollable result list, region-grouped. No top padding on the
              scroll container so the sticky group header sits flush at top:0 —
              otherwise rows scroll up through the gap above the header. */}
          <div ref={listRef} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "0 6px 6px" }}>
            {flat.length === 0 ? (
              <div
                style={{
                  height: "100%", display: "flex",
                  alignItems: "center", justifyContent: "center",
                  padding: "16px 12px", fontSize: 13, color: C.textMuted,
                  textAlign: "center",
                }}
              >
                No timezones match “{query}”.
              </div>
            ) : (
              groups.map(([region, list]) => (
                <div key={region}>
                  {/* Sticky region header. An OPAQUE band (own background +
                      negative side margins to span the container's 6px padding)
                      so rows scrolling underneath are fully covered, never
                      peeking through above the label. */}
                  <div
                    style={{
                      position: "sticky", top: 0, zIndex: 1,
                      margin: "0 -6px", padding: "9px 17px 7px",
                      // Uppercase group-header, same treatment as FaceSection /
                      // VoiceSection (700 / 0.09em), nudged to 11px to read
                      // comfortably above the larger 13px rows.
                      fontSize: 11, fontWeight: 700,
                      letterSpacing: "0.09em", textTransform: "uppercase", color: C.textDim,
                      background: C.surface, borderBottom: `1px solid ${C.border}`,
                    }}
                  >
                    {region}
                  </div>
                  {list.map((o) => {
                    const idx = flatIdx++;
                    const isSel = o.value === selected;
                    const isHi = idx === highlight;
                    return (
                      <div
                        key={o.value}
                        data-idx={idx}
                        role="option"
                        aria-selected={isSel}
                        onMouseEnter={() => setHighlight(idx)}
                        onClick={() => pick(o.value)}
                        style={{
                          display: "flex", alignItems: "center", justifyContent: "space-between",
                          gap: 8, padding: "10px 12px", borderRadius: 8, cursor: "pointer",
                          fontSize: 13,
                          background: isHi ? C.surface : "transparent",
                          color: isSel ? C.amber : C.text,
                          fontWeight: isSel ? 600 : 400,
                        }}
                      >
                        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {o.label}
                        </span>
                        {isSel && <span style={{ flexShrink: 0, fontSize: 12 }}>✓</span>}
                      </div>
                    );
                  })}
                </div>
              ))
            )}
          </div>
        </div>
      </div>,
      document.body,
    );

  return (
    <SectionCard id="timezone" title="Timezone" active={active}>
      {loading ? (
        <div style={{ fontSize: 12, color: C.textMuted }}>Loading…</div>
      ) : (
        <>
          <div style={{ fontSize: 12.5, color: C.textDim, marginBottom: 12, lineHeight: 1.6 }}>
            The device's local time zone. Used for quiet hours, daily history
            buckets, and the assistant's sense of time. Applies immediately — no
            restart needed.
          </div>

          <div style={{ marginBottom: 8 }}>
            <label htmlFor="timezone-button" style={LABEL_STYLE}>
              Zone (current: <span style={{ color: C.amber }}>{current || "?"}</span>)
            </label>

            {/* Trigger: shows the selected zone, opens the picker modal. */}
            <button
              id="timezone-button"
              type="button"
              onClick={() => !applying && setOpen(true)}
              disabled={applying}
              aria-haspopup="dialog"
              aria-expanded={open}
              style={{
                ...INPUT_STYLE,
                display: "flex", alignItems: "center", justifyContent: "space-between",
                gap: 8, textAlign: "left",
                opacity: applying ? 0.6 : 1,
                cursor: applying ? "not-allowed" : "pointer",
              }}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {selectedLabel || "Select a timezone…"}
              </span>
              <span style={{ color: C.textMuted, fontSize: 11, flexShrink: 0 }}>▼</span>
            </button>
          </div>

          {preview && (
            <div style={{ fontSize: 12, color: C.textMuted }}>
              Local time there now: <span style={{ color: C.text }}>{preview}</span>
            </div>
          )}

          {/* Picking a zone in the modal applies it immediately, so there's no
              Apply button. This line is the only async affordance: it shows
              while the POST is in flight. */}
          {applying && (
            <div style={{ marginTop: 8, fontSize: 12, color: C.amber }}>Applying…</div>
          )}

          {modal}
        </>
      )}
    </SectionCard>
  );
}
