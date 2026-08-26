import { useCallback, useEffect, useState } from "react";
import { CalendarClock, Pencil, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { C, SectionCard } from "@/components/setup/shared";
import {
  createSchedule, deleteSchedule, listSchedules, runScheduleNow, updateSchedule,
} from "@/lib/api";
import type { ScheduleCadence, ScheduleItem } from "@/lib/api";
import { ScheduleEditor, bodyFromDraft, draftFromSchedule } from "./ScheduleEditor";
import type { ScheduleDraft } from "./ScheduleEditor";

// Scheduled tasks on the device itself — list, create, edit, pause, delete,
// plus a local "Run now" per row.
//
// The cloud stays AUTHORITATIVE. Edits made here are PROPOSALS: each one is
// queued on the device (system/schedule/intent.go), published as
// schedule.mutate, and applied by the backend under an idempotency key and a
// compare-and-swap. What comes back is an ordinary full-state schedule.sync,
// which remains the single path by which this device's schedules.json changes.
//
// Two consequences are visible in this UI and are deliberate:
//
//   - a row can be marked "Syncing" / "Removing" while its proposal is in
//     flight, and on an offline device it stays that way until reconnect;
//   - a newly created task is NOT armed until the backend confirms it, so it
//     shows as pending rather than counting down to a next run.

// 0=Sunday..6=Saturday — matches the wire's convention (Go's time.Weekday),
// NOT ISO-8601 (where Monday=1). The two conventions only disagree on
// Sunday, so this table is indexed directly by the raw wire value.
const WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function weekdayLabel(d: number): string {
  const idx = d === 7 ? 0 : d; // 7 is an accepted alias for Sunday (0)
  return WEEKDAY_LABELS[idx] ?? `day ${d}`;
}

function ordinal(n: number): string {
  const rem100 = n % 100;
  if (rem100 >= 11 && rem100 <= 13) return `${n}th`;
  switch (n % 10) {
    case 1: return `${n}st`;
    case 2: return `${n}nd`;
    case 3: return `${n}rd`;
    default: return `${n}th`;
  }
}

// formatMs renders an INTERVAL DURATION IN MILLISECONDS (every_ms — the
// backend has already shipped a 1000x seconds/milliseconds bug on this exact
// field once, see spec.go's EveryMs doc comment) as a short human phrase.
function formatMs(ms: number): string {
  const minute = 60_000, hour = 3_600_000, day = 86_400_000;
  if (ms >= day && ms % day === 0) {
    const n = ms / day;
    return `${n} day${n === 1 ? "" : "s"}`;
  }
  if (ms >= hour && ms % hour === 0) {
    const n = ms / hour;
    return `${n} hour${n === 1 ? "" : "s"}`;
  }
  if (ms >= minute) {
    const n = Math.round(ms / minute);
    return `${n} minute${n === 1 ? "" : "s"}`;
  }
  return `${Math.round(ms / 1000)} second${Math.round(ms / 1000) === 1 ? "" : "s"}`;
}

// formatDeviceTime renders an RFC3339 instant in the device's own timezone
// (not the viewing browser's) — next_run_at/last_run_at were computed in the
// device's tz (schedule.Store.Timezone), so displaying them in any other zone
// would show a wall-clock time that doesn't match what the device itself will
// actually do. Returns null for a missing/unparseable instant so callers can
// choose their own fallback copy ("Never" vs "Not scheduled" read differently).
function formatDeviceTime(iso: string | undefined, tz: string): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  try {
    return new Intl.DateTimeFormat(undefined, {
      timeZone: tz || undefined, dateStyle: "medium", timeStyle: "short",
    }).format(d);
  } catch {
    return d.toLocaleString();
  }
}

// cadenceSummary renders one schedule's cadence as a single short line. Kept
// deliberately simple — this renders on a small device screen, not a full
// calendar editor (there is no editor at all: see the file doc comment).
function cadenceSummary(cadence: ScheduleCadence, tz: string): string {
  switch (cadence.repeat) {
    case "daily":
      return cadence.time ? `Daily at ${cadence.time}` : "Daily";
    case "weekly": {
      const days = (cadence.days ?? []).map(weekdayLabel).join(", ");
      const at = cadence.time ? ` at ${cadence.time}` : "";
      return days ? `Weekly on ${days}${at}` : `Weekly${at}`;
    }
    case "monthly": {
      const at = cadence.time ? ` at ${cadence.time}` : "";
      return cadence.day_of_month ? `Monthly on the ${ordinal(cadence.day_of_month)}${at}` : `Monthly${at}`;
    }
    case "interval":
      return cadence.every_ms ? `Every ${formatMs(cadence.every_ms)}` : "Interval";
    case "once": {
      const at = formatDeviceTime(cadence.at, tz);
      return at ? `Once, at ${at}` : "Once";
    }
    case "manual":
      return "Manual only — no automatic schedule";
    default:
      return cadence.repeat;
  }
}

export function ScheduledSection({ active }: { active: boolean }) {
  const [schedules, setSchedules] = useState<ScheduleItem[]>([]);
  const [timezone, setTimezone] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  // null = editor closed; "new" = creating; otherwise the id being edited.
  const [editing, setEditing] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const r = await listSchedules();
      setSchedules(r.schedules);
      setTimezone(r.timezone);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load schedules.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // The chat composer's "New scheduled task" links here with ?new=1 so the
  // create form is already open on arrival. The flag is consumed once (the URL
  // is rewritten) so a later reload does not silently reopen the editor over
  // whatever the user is doing.
  useEffect(() => {
    if (!active) return;
    const params = new URLSearchParams(window.location.search);
    if (params.get("new") !== "1") return;
    setEditing("new");
    params.delete("new");
    const qs = params.toString();
    window.history.replaceState(
      null, "",
      `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash}`,
    );
  }, [active]);

  // While a proposal is in flight the row shows "Syncing"; the backend's
  // confirmation arrives asynchronously over MQTT, so poll briefly to pick it
  // up. Only while something is actually pending — a settled list does no
  // polling at all, which is what keeps an idle Settings page quiet.
  const hasPending = schedules.some((s) => s.pending);
  useEffect(() => {
    if (!hasPending) return;
    const t = setInterval(() => { void refresh(); }, 3000);
    return () => clearInterval(t);
  }, [hasPending, refresh]);

  async function handleSave(draft: ScheduleDraft) {
    setSaving(true);
    try {
      if (editing === "new") {
        await createSchedule(bodyFromDraft(draft));
        toast.success("Task queued — it will run once the app confirms it.");
      } else if (editing) {
        await updateSchedule(editing, bodyFromDraft(draft));
        toast.success("Change queued.");
      }
      setEditing(null);
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to save.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(sch: ScheduleItem) {
    if (!window.confirm(`Delete "${sch.name}"? It keeps running until the app confirms the removal.`)) {
      return;
    }
    try {
      await deleteSchedule(sch.id);
      toast.success("Removal queued.");
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete.");
    }
  }

  // Pause/resume is an ordinary update of `enabled` — the same proposal path
  // as any other edit, so it is subject to the same confirmation.
  async function handleToggleEnabled(sch: ScheduleItem) {
    try {
      await updateSchedule(sch.id, { enabled: !sch.enabled });
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to update.");
    }
  }

  async function handleRunNow(sch: ScheduleItem) {
    setRunning(sch.id);
    try {
      const result = await runScheduleNow(sch.id);
      // Reflect the outcome locally rather than refetching the whole list —
      // RunNow only ever touches last-run bookkeeping (never cadence or
      // next_run_at, see Runner.RunNow's doc comment on the device), so this
      // is a complete, precise update, not an approximation.
      setSchedules((prev) => prev.map((s) => s.id === sch.id
        ? { ...s, last_run_at: result.started_at, last_run_status: result.status }
        : s));
      if (result.status === "success") {
        toast.success(`Ran "${sch.name}".`);
      } else {
        toast.error(`"${sch.name}" failed: ${result.summary}`);
      }
    } catch (err) {
      const status = (err as { status?: number } | undefined)?.status;
      const msg = status === 409
        ? "Agent is busy right now — try again shortly."
        : err instanceof Error ? err.message : "Failed to run.";
      toast.error(msg);
    } finally {
      setRunning(null);
    }
  }

  const BTN: React.CSSProperties = {
    padding: "6px 14px", borderRadius: 8, fontSize: 13, fontWeight: 500,
    cursor: "pointer", border: `1px solid ${C.border}`, background: C.surface,
    color: C.text,
  };

  return (
    <SectionCard id="scheduled" title="Scheduled" icon={<CalendarClock size={17} />} active={active}>
      <div style={{ fontSize: 12.5, color: C.textDim, marginBottom: 12, lineHeight: 1.6 }}>
        Tasks this device runs on a schedule. Changes made here are sent to the
        Autonomous app for confirmation, so a new task starts running once it
        syncs. "Run now" fires a task immediately without changing its schedule.
      </div>

      {editing !== "new" && (
        <button
          type="button"
          onClick={() => setEditing("new")}
          style={{
            ...BTN, display: "inline-flex", alignItems: "center", gap: 6, marginBottom: 12,
          }}
        >
          <Plus size={14} /> New task
        </button>
      )}

      {editing === "new" && (
        <ScheduleEditor
          initial={draftFromSchedule()}
          saving={saving}
          onSave={handleSave}
          onCancel={() => setEditing(null)}
        />
      )}

      {loading ? (
        <div style={{ fontSize: 12, color: C.textMuted }}>Loading…</div>
      ) : error ? (
        <div style={{ fontSize: 12, color: C.red }}>{error}</div>
      ) : schedules.length === 0 ? (
        editing !== "new" && (
          <div style={{ fontSize: 12, color: C.textMuted }}>
            No scheduled tasks yet. Create one here, or in the Autonomous app.
          </div>
        )
      ) : (
        schedules.map((sch) => {
          if (editing === sch.id) {
            return (
              <ScheduleEditor
                key={sch.id}
                initial={draftFromSchedule(sch)}
                saving={saving}
                onSave={handleSave}
                onCancel={() => setEditing(null)}
              />
            );
          }
          const lastRun = formatDeviceTime(sch.last_run_at, timezone) ?? "Never";
          // A pending CREATE has no next run to show: the device deliberately
          // does not arm a task the backend has not confirmed, so "Not
          // scheduled" would be misleading and a countdown would be a lie.
          const nextRun = sch.pending === "create"
            ? "Waiting to sync"
            : !sch.enabled
              ? "Paused"
              : formatDeviceTime(sch.next_run_at, timezone) ?? "Not scheduled";
          const isRunning = running === sch.id;
          const isPending = Boolean(sch.pending);
          return (
            <div
              key={sch.id}
              style={{
                padding: "12px 14px", marginBottom: 8,
                background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8,
              }}
            >
              <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8 }}>
                <div style={{ overflow: "hidden", minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: C.text, display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                    {sch.name}
                    <span style={{
                      fontSize: 10, fontWeight: 600, padding: "1px 7px", borderRadius: 999,
                      border: `1px solid ${C.border}`,
                      color: sch.enabled ? C.green : C.textMuted,
                      background: sch.enabled ? "var(--lm-green-dim)" : "transparent",
                    }}>
                      {sch.enabled ? "Enabled" : "Paused"}
                    </span>
                    {sch.pending && (
                      <span style={{
                        fontSize: 10, fontWeight: 600, padding: "1px 7px", borderRadius: 999,
                        border: `1px solid ${C.border}`, color: C.textDim,
                      }}>
                        {sch.pending === "delete" ? "Removing…" : "Syncing…"}
                      </span>
                    )}
                  </div>
                  {sch.instructions && (
                    <div style={{
                      fontSize: 11, color: C.textMuted, marginTop: 2,
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }}>
                      {sch.instructions}
                    </div>
                  )}
                </div>
                <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                  <button
                    type="button"
                    onClick={() => handleRunNow(sch)}
                    // A pending create has no server-side row yet, so there is
                    // nothing for the runner to look up — offering "Run now"
                    // would only produce a confusing 404.
                    disabled={isRunning || sch.pending === "create"}
                    style={{
                      ...BTN,
                      opacity: isRunning || sch.pending === "create" ? 0.4 : 1,
                      cursor: isRunning || sch.pending === "create" ? "not-allowed" : "pointer",
                    }}
                  >
                    {isRunning ? "Running…" : "Run now"}
                  </button>
                  <button
                    type="button"
                    title={sch.enabled ? "Pause" : "Resume"}
                    onClick={() => handleToggleEnabled(sch)}
                    disabled={isPending}
                    style={{ ...BTN, padding: "6px 10px", opacity: isPending ? 0.4 : 1 }}
                  >
                    {sch.enabled ? "Pause" : "Resume"}
                  </button>
                  <button
                    type="button"
                    title="Edit"
                    onClick={() => setEditing(sch.id)}
                    disabled={isPending}
                    style={{ ...BTN, padding: "6px 9px", opacity: isPending ? 0.4 : 1 }}
                  >
                    <Pencil size={13} />
                  </button>
                  <button
                    type="button"
                    title="Delete"
                    onClick={() => handleDelete(sch)}
                    disabled={isPending}
                    style={{ ...BTN, padding: "6px 9px", color: C.red, opacity: isPending ? 0.4 : 1 }}
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>

              <div style={{ fontSize: 12, color: C.textDim, marginTop: 8 }}>
                {cadenceSummary(sch.schedule, timezone)}
              </div>

              <div style={{ display: "flex", flexWrap: "wrap", gap: 14, fontSize: 11, color: C.textMuted, marginTop: 6 }}>
                <span>Next run: <span style={{ color: C.text }}>{nextRun}</span></span>
                <span>
                  Last run: <span style={{ color: C.text }}>{lastRun}</span>
                  {sch.last_run_status && (
                    <span style={{ color: sch.last_run_status === "success" ? C.green : C.red, marginLeft: 6 }}>
                      {sch.last_run_status === "success" ? "Succeeded" : "Failed"}
                    </span>
                  )}
                </span>
              </div>
            </div>
          );
        })
      )}
    </SectionCard>
  );
}
