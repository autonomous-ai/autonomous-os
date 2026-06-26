import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { C, SectionCard } from "@/components/setup/shared";
import { getTimezone, setTimezone } from "@/lib/api";

// Timezone picker. Like the phone "Date & Time" setting: pick an IANA zone from
// a single dropdown and apply. Not part of the form's "Save Changes" flow — it
// has its own Apply button hitting POST /api/device/timezone directly (writes
// /etc/localtime + /etc/timezone). The zone list comes from the device (system
// tzdata via timedatectl), so the UI never hardcodes it. One <select>, grouped
// by region via <optgroup> so the long list stays navigable.

const controlStyle = {
  width: "100%", boxSizing: "border-box" as const,
  background: C.surface, border: `1px solid ${C.border}`,
  borderRadius: 7, padding: "8px 11px",
  fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
};
const labelStyle = { display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 };

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

// labelOf is the full option text shown in the dropdown: "(GMT+7) Ho Chi Minh".
function labelOf(zone: string): string {
  const off = gmtOffset(zone);
  const city = cityOf(zone);
  return off ? `(${off}) ${city}` : city;
}

export function TimezoneSection({ active }: { active: boolean }) {
  const [current, setCurrent] = useState<string>("");
  const [zones, setZones] = useState<string[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  // Tick once a minute so the live-time preview stays current while the section
  // is open. Cheap; only the preview string depends on it.
  const [, setTick] = useState(0);

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

  // All zones grouped by region for <optgroup> rendering inside the one dropdown.
  // Each option carries a precomputed "(GMT+x) City" label; zones inside a region
  // are ordered by UTC offset (then name), like common web timezone pickers.
  const groups = useMemo(() => {
    const byRegion = new Map<string, { value: string; label: string; off: number }[]>();
    for (const z of zones) {
      const r = regionOf(z);
      (byRegion.get(r) ?? byRegion.set(r, []).get(r)!).push({
        value: z, label: labelOf(z), off: offsetMinutes(z),
      });
    }
    for (const list of byRegion.values()) {
      list.sort((a, b) => a.off - b.off || a.label.localeCompare(b.label));
    }
    return [...byRegion.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [zones]);

  const preview = useMemo(() => formatZoneTime(selected), [selected]);
  const valid = zones.length === 0 || zones.includes(selected);

  async function onApply() {
    if (!selected || selected === current || applying || !valid) return;
    setApplying(true);
    try {
      await setTimezone(selected);
      setCurrent(selected);
      toast.success(`Timezone set to ${selected}.`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to set timezone.");
    } finally {
      setApplying(false);
    }
  }

  return (
    <SectionCard id="timezone" title="Timezone" active={active}>
      {loading ? (
        <div style={{ fontSize: 12, color: C.textMuted }}>Loading…</div>
      ) : (
        <>
          <div style={{ fontSize: 11.5, color: C.textDim, marginBottom: 12, lineHeight: 1.6 }}>
            The device's local time zone. Used for quiet hours, daily history
            buckets, and the assistant's sense of time. Applies immediately — no
            restart needed.
          </div>

          <div style={{ marginBottom: 8 }}>
            <label htmlFor="timezone" style={labelStyle}>
              Zone (current: <span style={{ color: C.amber }}>{current || "?"}</span>)
            </label>
            <select
              id="timezone"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              disabled={applying}
              style={controlStyle}
            >
              {groups.map(([region, list]) => (
                <optgroup key={region} label={region}>
                  {list.map((z) => (
                    <option key={z.value} value={z.value}>{z.label}</option>
                  ))}
                </optgroup>
              ))}
            </select>
          </div>

          {preview && (
            <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 8 }}>
              Local time there now: <span style={{ color: C.text }}>{preview}</span>
            </div>
          )}

          <button
            type="button"
            onClick={onApply}
            disabled={applying || selected === current || !valid}
            style={{
              marginTop: 4,
              padding: "7px 18px", borderRadius: 7, fontSize: 12, fontWeight: 600,
              border: "none",
              cursor: applying || selected === current || !valid ? "not-allowed" : "pointer",
              background: applying || selected === current || !valid ? C.surface : C.amber,
              color: applying || selected === current || !valid ? C.textMuted : "#0C0B09",
              opacity: applying || selected === current || !valid ? 0.6 : 1,
              transition: "all 0.15s",
            }}
          >
            {applying ? "Applying…" : selected === current ? "Active" : "Apply"}
          </button>
        </>
      )}
    </SectionCard>
  );
}
