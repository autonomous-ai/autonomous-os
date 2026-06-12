# Agent session compaction — how it works and why it can override SKILL.md

> **Short version:** The agentic runtime auto-compacts the agent session when conversation tokens approach ~80k. The compaction result (a text `summary`) is then injected at the top of every subsequent turn's prompt until the next compact. If rules are accidentally copied or generalized into that summary, they can override the loaded `SKILL.md` — because the summary sits earlier in the prompt and is framed as "established context."
>
> This doc is the reference linked from the **📋 Summary** button in Flow Monitor (modal: `os/services/web/src/pages/monitor/FlowSection/CompactionModal.tsx`).

## Why compaction exists

The agentic runtime keeps a long conversation history. Each turn is the union of `user event`, `thinking`, `tool_call`, `tool_result`, and `assistant reply` entries — all stored in the session `.jsonl`. Over hours of activity, the tokens balloon. Once total context approaches **~80k tokens**, the LLM cannot fit any more input, so the runtime (or the OS server — see triggers below) performs a compaction: condense older entries into a single summary text, drop the originals, keep working.

## Compaction record

Compactions are stored as a single JSONL line inside the active session file. Example:

```
/root/.openclaw/agents/main/sessions/<sessionId>.jsonl
```

Structure of the record (trimmed):

```json
{
  "type": "compaction",
  "id": 17170331,
  "parentId": "369818c9",
  "timestamp": "2026-04-24T03:21:30.305Z",
  "summary": "<full summary text, ≤ ~16000 chars>",
  "firstKeptEntryId": 17170331,
  "tokensBefore": 80458,
  "details": {
    "readFiles": ["...", "KNOWLEDGE.md", "..."],
    "modifiedFiles": ["..."]
  },
  "fromHook": true
}
```

Notable fields:

| Field | Meaning |
|---|---|
| `summary` | The text injected at the top of every subsequent turn prompt. This is what the UI modal shows. |
| `firstKeptEntryId` | Split marker: entries before this id are replaced by `summary`; entries at/after remain. |
| `tokensBefore` | Total context size right before compaction fired. |
| `details.readFiles` | Files fed into the compaction prompt itself (KNOWLEDGE.md, HEARTBEAT.md, active SKILL.md…). Distortion can come from any of these. |
| `fromHook` | `true` when triggered by a hook; see triggers below. |

## Compaction flow

1. Trigger fires (see next section) — `tokens ≥ 80k`.
2. The runtime reads recent conversation history plus the files listed in `details.readFiles`.
3. A separate LLM call summarizes that input into one text string (≤ ~16000 chars observed — hard cap).
4. The compaction record is appended to the session `.jsonl` with `type:"compaction"`.
5. From the next turn onward, entries before `firstKeptEntryId` are **not** sent to the LLM anymore; the `summary` is spliced in at that position in the prompt.

## Prompt layout: before vs after

```
BEFORE compact                           AFTER compact
─────────────────                        ─────────────────
[system prompt]                          [system prompt]
[SOUL.md / AGENTS.md]                    [SOUL.md / AGENTS.md]
[history entries                         [📋 SUMMARY ~3-4k tokens]  ← NEW
 ... turn 1                              [kept entries after
 ... turn 2                               firstKeptEntryId]
 ...                                     [SKILL.md loaded per event]
 ... turn N]                             [new user event]
[SKILL.md loaded per event]
[new user event]
```

Because the summary is **earlier** in the prompt than the per-event-loaded SKILL.md, the LLM tends to weight it as "already-established context."

## Compact triggers (how to tell manual vs auto)

There are at least three ways a compaction can fire:

| Source | Trigger | Side-effects | Observed `fromHook` |
|---|---|---|---|
| **Runtime internal hook** | tokens ≥ 80k, server-side detection | — | `true` |
| **OS server RPC** (`os/services/server/openclaw/delivery/sse/handler_events.go:380-406`) | The OS server sees `u.TotalTokens > 80_000` on a lifecycle event, calls `agentGateway.CompactSession(sessionKey)` | TTS speaks *"Hold on, tidying up a bit."*; 2-minute cooldown via `h.compacting` atomic | unknown — needs verification against runtime source |
| **Manual / debug** | Someone invokes `sessions.compact` RPC directly (e.g. from a client tool) | — | likely `false` |

**Heuristic to distinguish on UI today:** if a record's `timestamp` is within a few seconds after a `"sessions.compact sent"` log line in the OS server's journal for the same `sessionKey`, it was OS-server-initiated. Otherwise the runtime's internal hook.

A future enhancement: the compaction modal could correlate the latest compact's timestamp against the OS server's log to label the trigger.

## Observed frequency (48h sample, main session)

| Pattern | Interval between compacts |
|---|---|
| Busy daytime | 1–3 h |
| Overnight idle | 10–13 h |
| Abnormal burst | multiple compacts within minutes at `tokensBefore ≈ 45–60k` (well below the 80k threshold) |

The abnormal burst pattern is unexplained — possibly a session restart / checkpoint restore fires the hook spuriously, or a downstream tool is re-issuing `sessions.compact`. Worth investigating when it recurs.

## Why the summary can make the agent go wrong

1. **Priority inversion.** Summary precedes SKILL.md in the prompt; LLM treats it as higher-priority fact.
2. **Generalization.** Summarization routinely turns narrow cases (e.g. *"drink activity for known user → warm acknowledgement"*) into general rules (*"known-user activity events → warm acknowledgement"*) — which the agent then applies to unrelated cases.
3. **Staleness.** Summary freezes field values at the moment of compaction. Example: SKILL.md updated `last=50` → `last=200`, but a summary from before the change still says `last=50` and the agent follows it until the next compact.
4. **Generational loss.** Each compaction reads the *previous* summary as input. Rule distortions get re-summarized → drift compounds, JPEG-save-JPEG style.
5. **Hard cap.** The summary is capped around 16000 characters (observed: three distinct records hit exactly that value). Content is dropped non-deterministically when the cap is reached.

When Flow Monitor shows the agent citing rules that `grep` cannot find in any `skills/**/SKILL.md`, the compaction summary is almost always the real source — not the loaded skill.

## Inspecting the active summary

**UI.** Flow Monitor header → **📋 Summary** button → modal shows `timestamp`, `summary chars`, `session file`, and the full summary text.

**API.** `GET /api/openclaw/compaction-latest?session=<key>` (default session key: `agent:main:main`). Response schema:

```json
{
  "status": 1,
  "data": {
    "found": true,
    "sessionKey": "agent:main:main",
    "sessionFile": "/root/.openclaw/agents/main/sessions/<id>.jsonl",
    "compactionCount": 18,
    "id": 17170331,
    "timestamp": "2026-04-24T03:21:30.305Z",
    "tokensBefore": 80458,
    "summaryChars": 14263,
    "summary": "...",
    "details": { "readFiles": ["..."], "modifiedFiles": ["..."] },
    "fromHook": true,
    "firstKeptEntryId": 17170331
  }
}
```

**Direct (Pi SSH).** All compaction records live in the session `.jsonl`. To pull them with timestamps and metadata:

```bash
sudo grep '"type":"compaction"' \
  /root/.openclaw/agents/main/sessions/<sessionId>.jsonl \
  | python3 -c 'import json,sys
for l in sys.stdin:
    d=json.loads(l)
    print(d["timestamp"], d.get("tokensBefore"), len(d.get("summary","")))'
```

## Related files

| File | Role |
|---|---|
| `os/services/server/openclaw/delivery/sse/handler_api_compaction.go` | HTTP handler: reads `sessions.json`, scans session `.jsonl` for newest `type:"compaction"`. |
| `os/services/server/openclaw/delivery/sse/handler_events.go` | OS-server-side RPC trigger (auto-compact when `TotalTokens > 80_000`, TTS notice, 2-min cooldown). |
| `os/services/internal/openclaw/service_chat.go` | `CompactSession(sessionKey)` — the `sessions.compact` RPC sender. |
| `os/services/domain/agent.go` | `AgentGateway.CompactSession` interface. |
| `os/services/web/src/pages/monitor/FlowSection/CompactionModal.tsx` | UI modal — shows timestamp, summary chars, session file, full summary text; links back to this doc. |
| `docs/flow-monitor.md` | Parent doc — cross-references this one. |

Vietnamese summary: [`docs/vi/agent-compaction_vi.md`](vi/agent-compaction_vi.md).
