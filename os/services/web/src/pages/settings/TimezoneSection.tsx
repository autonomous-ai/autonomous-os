import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { C, SectionCard } from "@/components/setup/shared";
import { getTimezone, setTimezone } from "@/lib/api";

// Timezone picker. Like the phone "Date & Time" setting: pick an IANA zone and
// apply. Not part of the form's "Save Changes" flow — it has its own Apply
// button hitting POST /api/device/timezone directly (writes /etc/localtime +
// /etc/timezone). The zone list comes from the device (system tzdata via
// timedatectl), so the UI never hardcodes it.

const inputStyle = {
  width: "100%", boxSizing: "border-box" as const,
  background: C.surface, border: `1px solid ${C.border}`,
  borderRadius: 7, padding: "8px 11px",
  fontSize: 12.5, color: C.text, outline: "none",
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

          <div style={{ marginBottom: 10 }}>
            <label htmlFor="timezone" style={labelStyle}>
              Zone (current: <span style={{ color: C.amber }}>{current || "?"}</span>)
            </label>
            <input
              id="timezone"
              list="timezone-options"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              disabled={applying}
              placeholder="e.g. Asia/Ho_Chi_Minh"
              autoComplete="off"
              spellCheck={false}
              style={inputStyle}
            />
            <datalist id="timezone-options">
              {zones.map((z) => <option key={z} value={z} />)}
            </datalist>
          </div>

          {preview && (
            <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 6 }}>
              Local time there now: <span style={{ color: C.text }}>{preview}</span>
            </div>
          )}
          {!valid && (
            <div style={{ fontSize: 11, color: C.red, marginBottom: 6 }}>
              Unknown zone — pick one from the list.
            </div>
          )}

          <button
            type="button"
            onClick={onApply}
            disabled={applying || selected === current || !valid}
            style={{
              marginTop: 8,
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
