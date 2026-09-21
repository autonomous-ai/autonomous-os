import type { Turn } from "./types";

// Flow events arrive as dynamic JSON over SSE: the key set differs per event
// type and the device nests forwarded payloads under `data`
// (docs/flow-monitor.md). Every read below coerces with String/Number/Boolean
// or an Array.isArray guard, so `unknown` costs one cast and buys real safety.
// Keeping this module free of value imports is deliberate: it is what lets
// `node --test` load it directly (see system/web/tests/memory.test.mjs).
type FlowDetail = Record<string, unknown>;

export interface TurnMemoryInfo {
  // Fingerprint attached at lifecycle_start: file → {size, sha8}. Bytes.
  files: Record<string, { size: number; sha8: string }>;
  // memory_changed events that landed inside this turn (the agent wrote memory).
  // `execute` is the guard's mode: true = the quarantined blocks were really
  // removed; false = observe-only (`agent.memory_guard=false`), the file still
  // carries them and `quarantined` is what the guard WOULD have removed.
  // `runtime` is which runtime tree the write happened in — the guard sweeps
  // all six, but `files` above is the ACTIVE runtime's only (#463).
  changed: { file: string; runtime: string; quarantined: number; reasons: string[]; execute: boolean }[];
}

// Fixed display order for the memory fingerprint — the order the runtime loads
// them in, not Object.entries order, so a glance across turn cards compares
// like with like.
export const MEMORY_FILE_ORDER = ["USER.md", "MEMORY.md", "KNOWLEDGE.md"] as const;

export function orderedMemoryFiles(
  files: TurnMemoryInfo["files"],
): [string, { size: number; sha8: string }][] {
  const known = MEMORY_FILE_ORDER.filter((n) => n in files).map((n) => [n, files[n]] as [string, { size: number; sha8: string }]);
  const rest = Object.entries(files).filter(([n]) => !(MEMORY_FILE_ORDER as readonly string[]).includes(n));
  return [...known, ...rest];
}

// Memory the turn ran with, and whether it wrote any. Both come from flow
// events: `lifecycle_start.data.memory` (published by the OS memory guard) and
// `memory_changed` (emitted by its fsnotify watch ~2 s after the write, tagged
// with whatever trace is active then — or trace-less if the turn has already
// ended). Null when neither is present (older os-server, or no guard yet).
export function turnMemoryState(turn: Turn): TurnMemoryInfo | null {
  let files: TurnMemoryInfo["files"] | null = null;
  const changed: TurnMemoryInfo["changed"] = [];
  for (const ev of turn.events) {
    if (ev.type !== "flow_event") continue;
    const d = ev.detail as FlowDetail | undefined;
    const data = (d?.data ?? {}) as FlowDetail;
    if (d?.node === "lifecycle_start" && data.memory && typeof data.memory === "object") {
      files = data.memory as TurnMemoryInfo["files"];
    }
    if (d?.node === "memory_changed") {
      changed.push({
        file: String(data.file ?? ""),
        // "" when the guard could not name a runtime (a path outside the
        // adapter registry, migrate_persona/memory_guard_files.go:232).
        // Better silent than wrong — the label omits it entirely.
        runtime: String(data.runtime ?? ""),
        quarantined: Number(data.quarantined ?? 0),
        reasons: Array.isArray(data.reasons) ? data.reasons.map(String) : [],
        // Missing on an older os-server that always executed: default true.
        execute: data.execute === undefined ? true : Boolean(data.execute),
      });
    }
  }
  if (!files && changed.length === 0) return null;
  return { files: files ?? {}, changed };
}
