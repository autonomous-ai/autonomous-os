# Flow Monitor

The Flow Monitor is an observability layer for tracking agent turns end-to-end. It records events to daily JSONL files (`local/flow_events_YYYY-MM-DD.jsonl`) and streams them to the web UI via SSE.

**Important**: The flow monitor is purely observational. It does NOT affect device behavior, agent communication, TTS, LED, or any business logic.

## Architecture

```
LeLamp (Python)                    Lumi Server (Go)                     Web UI (React)
  sensing event ──POST──→ SensingHandler ──flow.Start/End──→ JSONL file
                            │                                    ↓
                            └─ agentGateway.SendChat ──→ OpenClaw (WS)
                                                           │
                          SSE Handler ←── WS events ───────┘
                            │
                            ├─ flow.Log("lifecycle_*") ──→ JSONL file ──→ /flow-stream (SSE)
                            ├─ flow.Log("tool_call")                         ↓
                            ├─ flow.Log("tts_send")                    lumi/web/.../Monitor.tsx
                            └─ monitorBus.Push() ──→ /openclaw/events (SSE)  └─ groupIntoTurns()
```

## Per-Event Run ID (refactored from global trace)

### Before (global trace)
```go
flow.SetTrace(runID)           // set global, affects ALL subsequent events
flow.Log("lifecycle_end", data) // picks up global trace
flow.ClearTrace()              // clear global
```

Problems:
- Concurrent turns override each other's trace
- Server restart loses in-memory trace
- `ClearTrace()` in goroutine races with next `SetTrace()`

### After (per-event run ID)
```go
flow.Log("lifecycle_end", data, payload.RunID)  // explicit per-event
flow.Log("tool_call", data, payload.RunID)      // each event carries its own ID
```

- `Start()`, `End()`, `Log()` accept optional variadic `runID` parameter
- If provided, overrides the global trace for that event only
- Global `SetTrace`/`GetTrace` retained for the Telegram-detection heuristic
- `ClearTrace()` decrements active trace (ref-counted), called after OpenClaw `lifecycle_end`

### Telegram Detection Heuristic

When `lifecycle_start` arrives without an active device trace (`flow.GetTrace() == ""`), the handler checks if it's a channel-initiated turn (Telegram/Slack). Lumi-originated `chat.send` turns are excluded via `lumi-chat-*` (and legacy `lumi-sensing-*`) so they are not mis-labeled as Telegram when the trace was lost.

#### Fetching user message content via `chat.history` RPC

OpenClaw's chat stream **never broadcasts `role:"user"` events** — it only emits `role:"assistant"` (delta/final/error). To get the user message text and sender name, Lumi calls the `chat.history` WebSocket RPC on the same WS connection used for events:

```
→  {"type":"req","id":"history-1","method":"chat.history",
    "params":{"sessionKey":"agent:main:telegram:group:-5139766247","limit":20}}

←  {"type":"res","id":"history-1","ok":true,
    "payload":{"sessionKey":"...","sessionId":"...","messages":[
      {"role":"user","content":[{"type":"text","text":"dừng phát nhạc đi"}],
       "senderLabel":"Leo (158406741)"},
      {"role":"assistant","content":[...]},
      ...
    ],"thinkingLevel":"low"}}
```

Implementation details:

- **Async goroutine**: The fetch runs in a separate goroutine because calling it synchronously inside the WS read loop handler would deadlock (the read loop blocks waiting for the handler to return, but the RPC response can only arrive after the handler returns).
- **Pending RPC tracking**: `pendingRPC map[string]chan json.RawMessage` in `internal/openclaw/service.go` matches `type:"res"` frames to waiting callers by request ID. `dispatchRPCResponse()` hooks into the read loop before event handling.
- **Two-phase emit**: First `chat_input` fires immediately with a neutral `[chat]` placeholder (no message yet). After the goroutine gets the history, a second `chat_input` fires with the actual message text and a label chosen by `senderLabel` / message-prefix inspection — the UI picks up the one with content.
- **Label routing (second emit)**: (1) `senderLabel` non-empty → `[telegram:Gray]` (real channel user). (2) `senderLabel` empty + message matches a Lumi-internal prefix → `[voice]` / `[emotion]` / `[activity]` / `[wellbeing]` / `[music]` / `[sensing]` / `[system]` (sensing or voice event Lumi posted via chat.send that OpenClaw merged into this UUID host turn via steer mode). (3) Otherwise → generic `[chat]`. Previously every UUID channel-turn was unconditionally labelled with the configured channel (`[telegram]`), mis-attributing steer-merged self-fire turns to Telegram.
- **Best-effort**: 3-second timeout. If the fetch fails, the turn stays at the generic `[chat]` placeholder — better than mis-attributing to a specific channel.
- **Heartbeat noise**: OpenClaw heartbeat cron (every 30m) also triggers `lifecycle_start`. The last `role:"user"` message in those turns will be the heartbeat system prompt (starts with `"System:"`), not a real user message.
- **Token usage**: `chat.history` is also called on `lifecycle_end` to fetch token usage. OpenClaw `lifecycle_end` events do not include `usage` data. The last `role:"assistant"` message in the history response contains `usage: {input, output, totalTokens, cacheRead, cacheWrite}` for the completed turn. This is emitted as a `token_usage` flow event with `source: "chat_history"`.

## Run ID Format & Mapping

```
sendChat() generates:
  reqID           = "chat-1"                       (WS message ID, local counter)
  idempotencyKey  = "lumi-chat-1-1774841927380"    (sent to OpenClaw, globally unique; not "sensing-only" — any outbound chat from Lumi uses this prefix)

sendChat returns idempotencyKey → used as trace_id in flow events
```

**OpenClaw run_id behavior depends on version:**
- **5.2** (and rare paths in 5.4): assigns its own UUID (e.g., `a8a51f3c-b44f-434b-a4c9-cd1a2a1e3c30`) — Lumi must map UUID → idempotencyKey.
- **5.4** (majority): echoes the idempotencyKey directly as the runId (verified in `src/gateway/server-methods/chat.ts:2002`, `clientRunId = p.idempotencyKey`). The lifecycle runId IS already the device trace; no map is needed.
- **Mixed within one session**: a single chat.send can produce up to two lifecycle phases — Phase 1 with the echoed idempotencyKey, Phase 2 with a fresh UUID for the actual embedded run (drain/burst pattern). Both must be handled.

**Solution: `pendingChatTrace` FIFO + search-and-remove** in the SSE handler:
1. Sensing handler allocates `NextChatRunID()`, then calls `flow.SetTrace(idempotencyKey)` **before** `flow.Start("sensing_input", ...)` so the JSONL `enter` line uses the same `trace_id` as `chat_send` for that POST. (Calling `SetTrace` only after `SendChatMessage` used to leave `enter` tagged with the **previous** turn's id — ghost turns and mismatched Pair exports.)
2. After `chat.send` succeeds, Lumi pushes the idempotencyKey onto `pendingChatQueue` (FIFO).
3. On `lifecycle_start` the handler branches on `payload.RunID` format:
   - **Lumi-format** (`lumi-chat-*`): the runId IS the device trace. Call `RemovePendingChatTraceByRunID(payload.RunID)` to drop the matching queue entry. **No map** is created. Removing (instead of skipping) is critical: a leftover entry would later be FIFO-popped by an unrelated UUID lifecycle and shift every subsequent mapping by one.
   - **UUID** (`a8a51f3c-...`): unknowable from Lumi side. FIFO-pop the oldest queue entry and store mapping `UUID → idempotencyKey`. Stale entries (>2 min) are pruned from the head before popping.
4. All subsequent **agent-stream** events (`lifecycle`, `tool`, `thinking`, `assistant` deltas, `tts_send`) use `resolveRunID(payload.RunID)` so `trace_id` matches the device key.
5. **Chat stream** events (`case "chat"`: user/assistant text from the parallel chat feed) also call `resolveRunID` for `flow.Log` and monitor `RunID`. Without this, OpenClaw could emit the **UUID** in chat payloads while JSONL from step 4 used the **device id** — the Monitor would split one turn into two IDs.

### Correlation logs (grep: `flow correlation`)

Structured `slog.Info` lines for end-to-end ID alignment (device idempotency key = `lumi-chat-*`):

| `op` | `section` (when set) | When |
|------|------------------------|------|
| `ws_chat_send` | `lumi_to_openclaw_ws` | Every `chat.send` from Lumi (`device_run_id` = idempotency key). |
| `lelamp_agent_out` | `lelamp_to_openclaw` | Sensing handler after `SetTrace` + `agent_call` (same `device_run_id`). |
| `openclaw_uuid_map` | `openclaw` | `lifecycle_start`: OpenClaw UUID stored → device id. |
| `chat_run_resolve` | `openclaw_chat` | Chat stream event where `resolveRunID` changed the id (UUID → device). |

## Turn Grouping (Frontend)

`groupIntoTurns()` in `Monitor.tsx` groups events into turns:

1. **Turn start detection**: `sensing_input`, `chat_input`, `ambient_action`, `schedule_trigger`
2. **Run ID grouping**: events with same `runId` stay in same turn
3. **Fragment merging**: turns sharing same `runId` get merged (handles split events)
4. **Placeholder type upgrade**: channel `chat_input` fires twice (see "Two-phase emit" above). The first emit pins `turn.type = "chat"` from the `[chat]` placeholder. When the second emit lands with the real message, `isTurnStart` re-derives a specific type (`emotion.detected`, `speech_emotion.detected`, `voice`, `telegram`, …) from the message prefix; `groupIntoTurns` upgrades `turn.type` only if it was still the `"chat"` (or `"unknown"`) placeholder — preserving any already-specific classification. The `[speech_emotion]` prefix maps to `speech_emotion.detected` and is categorized under the `mic` source (voice-driven), not `cam`, even though the label contains "emotion".
5. **Stitching**: orphaned output-only turns merge with nearby input-only turns (handles server restart splits)
6. **Session breaks**: >60s gap between turns marks a session boundary

### Stitching Rules

| Previous Turn | Current Turn | Condition | Action |
|---|---|---|---|
| Telegram fallback (no message) | Agent output | <30s gap | Merge |
| Sensing input (no output) | Orphan output (no input) | <30s gap | Merge |

## Turn Pipeline (SVG `FlowDiagram`)

Rendered by `FlowDiagram` in `lumi/web/src/pages/Monitor.tsx`. The diagram is **observational only** (zoom/pan, node highlights from recent events). Three **tinted cluster** regions group nodes:

| Region | Color (theme) | Stages |
|--------|----------------|--------|
| **Lumi Server** | Teal (`--lm-teal`) | `intent_check`, `local_match`, `schedule_trigger`, `lumi_gate` |
| **LeLamp** | Amber (`--lm-amber`) | `mic_input`, `cam_input`, `hw_emotion`, `hw_led`, `hw_servo`, `tts_speak` |
| **OpenClaw** | Blue (`--lm-blue`) | `agent_call`, `telegram_input`, `tool_exec`, `agent_thinking`, `agent_response`, `tg_out` |

### Lumi Server (top band)

- **Intent** and **Local** sit on the **same top row** (left to right).
- **Cron** (`schedule_trigger`) is a **Lumi** stage (timer owned by Lumi, not OpenClaw). It shares the **same top `y`** as Intent / Local but uses **`x` aligned with `agent_call`** so Cron → Agent reads as a **vertical column** in the SVG.
- Cron is **not** inside the OpenClaw cluster; only the shared `x` is for layout.

### LeLamp (left column)

- **MIC** and **CAM** are input nodes (top of LeLamp section).
- Output nodes are stacked vertically in a single column:
  - **EMO** (`hw_emotion`) — `/emotion` calls (coordinated LED + servo + display eyes)
  - **LED** (`hw_led`) — `/led/solid`, `/led/effect`, `/scene`, `/led/off`
  - **SERVO** (`hw_servo`) — `/servo/aim`, `/servo/play`
  - **TTS** (`tts_speak`) — `/voice/speak`, text-to-speech output
- These represent direct hardware calls from OpenClaw tools that bypass Lumi.

### OpenClaw layout rules (column + row)

These are the **stable rules** for nodes inside the OpenClaw rectangle; `positions` in `Monitor.tsx` follow this grid.

**Columns (left → right)**

| Col | Stages |
|-----|--------|
| **1** | Tool Exec, Response (stacked — Response under Tool) |
| **2** | Agent Call (top) → Event Pipeline rect (middle) → Response (bottom). The pipeline contains rows for thinking / assistant / tool / lifecycle / compaction / error events in order; see `docs/debug/flow-monitor-pipeline.md` for aggregation rules and the rationale for collapsing the previous 3-node `LLM Start / Thinking / Tool Exec` chain into one rect. |
| **3** | Telegram In (`TG IN`) |

**Rows (top → bottom)**

| Row | Rule |
|-----|------|
| **1** | **Agent** and **TG In** share one horizontal row (TG → Agent). |
| **2** | **Thinking** and **Tool** share one horizontal row (flow Think → Tool, left to right). |
| **3** | **Response** under column 1 (below Tool). |

**ASCII grid (OpenClaw only)**

```
              Col1        Col2        Col3
         ┌──────────┬──────────┬──────────┐
    Row1 │          │  Agent   │  TG In   │
         ├──────────┼──────────┼──────────┤
    Row2 │   Tool   │ Thinking │          │
         ├──────────┴──────────┴──────────┤
    Row3 │   Response (under Tool)        │
         └────────────────────────────────┘
```

### Approximate coordinates (for layout maintenance)

Values are the **node center** `(x, y)` in the SVG view box (see `positions` in `Monitor.tsx`). Adjust clusters if you move nodes.

| Stage | `(x, y)` | Note |
|-------|----------|------|
| `intent_check` | `(80, 50)` | Lumi top |
| `local_match` | `(200, 50)` | Lumi top |
| `schedule_trigger` | `(800, 50)` | Lumi top; `x` = Agent column |
| `lumi_gate` | `(400, 570)` | Lumi; between LeLamp and OpenClaw |
| `mic_input` | `(-40, 240)` | LeLamp input |
| `cam_input` | `(80, 240)` | LeLamp input |
| `hw_emotion` | `(200, 390)` | LeLamp output; emotion calls |
| `hw_led` | `(200, 510)` | LeLamp output; LED control |
| `hw_servo` | `(200, 630)` | LeLamp output; servo motor |
| `tts_speak` | `(200, 750)` | LeLamp output; TTS |
| `agent_call` | `(800, 240)` | OpenClaw row 1 |
| `telegram_input` | `(1000, 240)` | OpenClaw row 1 |
| `tool_exec` | `(600, 390)` | OpenClaw row 2, col 1 |
| `agent_thinking` | `(800, 390)` | OpenClaw row 2, col 2 |
| `agent_response` | `(600, 570)` | OpenClaw row 3, col 1 |
| `tg_out` | `(1000, 570)` | OpenClaw row 3; Telegram output |

### Edges

```
mic_input → intent_check → local_match → hw_emotion / hw_led / hw_servo / tts_speak
cam_input → intent_check → agent_call
schedule_trigger → agent_call
telegram_input → agent_call
agent_call → [Event Pipeline rect — thinking/assistant/tool rows] → agent_response
tool_exec → hw_emotion         (OpenClaw /emotion call → LeLamp)
tool_exec → hw_led             (OpenClaw /led/* or /scene call → LeLamp)
tool_exec → hw_servo           (OpenClaw /servo/* call → LeLamp)
tool_exec → lumi_gate          (Lumi listens: suppress TTS if music, pause ambient if LED)
agent_response → lumi_gate     (Lumi accumulates assistant text for TTS)
agent_response → tts_speak     (Direct TTS from response)
agent_response → tg_out        (Telegram/Slack output)
lumi_gate → tts_speak          (Gate passes if not suppressed → LeLamp TTS)
```

**Elbow routing**: Edges from `local_match` to output nodes (hw_emotion, hw_led, hw_servo, tts_speak) use elbow paths routed to the **left** of the output column to avoid crossing intermediate nodes.

### Event → node labels (runtime detail boxes)

Node info extracted from turn events:
- `sensing_input` → Sensing node (type + message). Detail: `{ type }`.
- `chat_send` → outbound `chat.send` from Lumi. Detail: `{ type, run_id, has_session, has_image, image_bytes, message }`. `type` is `"user"` for real user / sensing-driven input, or `"system"` for internal notifications (skill watcher, wake greeting). The WS RPC payload sent to OpenClaw is identical in both cases — `type` only labels the flow event so the UI can distinguish them. Auto-compact does **not** emit a `chat_send`; it calls the `sessions.compact` RPC directly via `CompactSession`.
- `sound_tracker` → pushed by LeLamp Python directly via `POST /api/monitor/event`. Appears alongside `sensing_input` turns to show escalation state:
  - `{ action: "silent", occurrence: 1 }` — forwarded, agent stays silent
  - `{ action: "persistent", occurrence: 3 }` — forwarded, agent will speak
  - `{ action: "drop" }` — dropped by dedup or suppression window
- `chat_input` → Telegram In node
- `intent_match` → Local Match node
- `lifecycle_start` → Agent Call node + first row in the Event Pipeline.
- `tool_call` → one Event Pipeline row per tool invocation, kind=`tool`,
  label=`tool · <name>`, with `start`/`result` phases collapsed into the
  row's duration. Outgoing HW edges (LED / servo / emotion / audio /
  lumi_gate) anchor at the pipeline's right edge.
- `lifecycle_end` → Response node + final row in the Event Pipeline.
- `tts_send` → TTS Speak + Output nodes (text from `detail.data.text`)
- `tts_suppressed` → 🔇 marker in Lumi gate column. `data.reason` discriminates: `channel_run` (real Telegram user turn — detected by `tg-` runID prefix synthesised in the `session.message` handler, or `channelRuns` map mark from chat.history fallback; reply fans out via OpenClaw session instead of the lamp speaker), `music_playing` (audio shares the speaker), `already_spoken` (built-in tts tool already routed), `web_chat` (Flow Monitor chat — reply shown in web UI only). Emitted *instead of* `tts_send` when the actual `SendToLeLampTTS` call is skipped — prevents the UI from misleadingly claiming TTS happened. Classifier uses positive evidence only: UUID runs from OpenClaw steer-mode self-fire, cron fires, and heartbeats are NOT `channel_run` and DO speak on the lamp.
- `token_usage` → Response node (token counts).

### NO_REPLY suppression

OpenClaw agent may respond with `NO_REPLY` (or truncated forms `NO`, `NO_RE`, `NO_...`) when it decides not to respond — typically for passive sensing events like sound/motion. These are suppressed by `isAgentNoReply()` in `handler.go`: no TTS playback, no output display. Matches: exact `"NO"`, or any string starting with `"NO_"` or `"NO_RE"` (case-insensitive after trim). Source: `lifecycle_end` payload if available, otherwise fetched from `chat.history` RPC on `lifecycle_end` (async goroutine, best-effort). OpenClaw `lifecycle_end` currently does not include usage data, so `chat.history` is the primary source.

## Stream summary events (`agent_*_token` / `thinking_*_token`)

Raw `assistant_delta` and `thinking` deltas are pushed to monitorBus (RAM) but **never written to JSONL** — the persist layer would otherwise grow by ~50–500 lines per turn. Since the Flow Monitor reads JSONL on reload, the pipeline rect for past turns would show no streaming rows.

To bridge that gap, the OpenClaw stream handler emits four lightweight summary flow events per run:

| Node | When | Payload (`data.*`) | Purpose |
|---|---|---|---|
| `agent_first_token` | First non-empty `assistant` delta in the run | `{run_id}` | TTFT marker — `ts` field = perceived reply latency moment |
| `agent_last_token` | `lifecycle.end` (drain accumulator) | `{run_id, text, chunks, chars}` | Closes the assistant streaming row in the pipeline rect |
| `thinking_first_token` | First non-empty `thinking` delta (extended-thinking only) | `{run_id}` | Same as above, for the thinking stream |
| `thinking_last_token` | `lifecycle.end` | `{run_id, text, chunks, chars}` | Same as above, for the thinking stream |

Maximum 4 extra JSONL lines per turn (often 0–2). Stream from OpenClaw is still called `"assistant"` in code (`handler_events.go: case "assistant"`); only the JSONL node names use the `agent_` prefix for consistency with existing `agent_thinking` / `agent_call` / `agent_response` nodes.

State lives in `OpenClawHandler.streamStats` (per-run counters + accumulated text), independent of `assistantBuf` (which serves TTS flush). Drained on `lifecycle.end`. See `recordAssistantDelta` / `recordThinkingDelta` / `drainStreamStats` in `handler_state.go`.

Frontend (`aggregateEvents` in `helpers.ts`) builds pipeline rows from `*_first_token` (opens a row) + `*_last_token` (closes it with `chunks`/`chars`). `extractTurnTiming` and `turnFirstTokenMs` both fall back to these markers when live deltas aren't in `turn.events`.

The legacy `llm_first_token` flow event that was previously removed for being "redundant with the pipeline aggregator" is effectively re-introduced here — split into the two streams (`agent_*` and `thinking_*`), because the aggregator can't observe streaming moments when raw deltas never reach JSONL.

## Turn Item Display

```
[icon] TYPE  PATH  STATUS  👤 user  ⏱ total  ⚡ ttft
id: run-id
IN   <input text>
OUT  🔊 <output text>
N events
```

- **IN**: extracted from `sensing_input` summary or `chat_input` detail.message
- **OUT**: from `intent_match` (local) or `tts_send` (agent). Intent match is authoritative and won't be overwritten by stale tts_send from different runs.
- **Path badge**: LOCAL (green) / AGENT (blue) — only set from events belonging to the same run
- **⏱ total**: `turn.startTime → turn.endTime` (full server-observed window: input event → lifecycle_end / tts_send / chat_final). Green ≤5s, amber ≤15s, red >15s.
- **⚡ TTFT** (time-to-first-token): `turn.startTime → first thinking/assistant_delta`. Matches the chat page Lumi-bubble stamp — the moment the user *sees* a reply begin. Gap between ⚡ and ⏱ = tail streaming + lifecycle close. Green ≤3s, amber ≤8s, red >8s. Hidden when no LLM stream (e.g., local intent match).

The two badges are meant to be read together: ⚡ is *perceived* latency (what the user feels), ⏱ is *server* latency (what ops sees). Big gap = lots of tail streaming; small gap = short reply or fast lifecycle close.

## Known Edge Cases

### 1. OpenClaw assigns different run_id
OpenClaw 5.2 (and rare 5.4 paths) generate a UUID for the embedded run; 5.4 mostly echoes the `idempotencyKey`. Mapping logic must handle both.
- **Fix**: SSE handler picks path by `payload.RunID` format — Lumi-format → search-and-remove queue entry; UUID → FIFO pop + map. See section above.
- **Edge case**: If server restarts between `sendChat` and `lifecycle_start`, the global trace is lost and no mapping is created. Frontend stitching handles this as a fallback.
- **Status**: Fixed for normal operation. Fallback stitching for restart edge case.

### 1a. Pending-trace orphan misattribution (regression in 0.0.465, fixed in 0.0.468)
A previous attempt (commit `1897dfee`) skipped the FIFO pop entirely when the runId was Lumi-format, but left the entry in the queue. The next UUID lifecycle then popped that orphan and was misattributed to it — observed pattern: Phase 1 reply + Phase 2 reply both rendered under the Phase 1 chat-N, with Phase 2's content actually belonging to the next chat in the drain.
- **Trigger**: drain/burst flushes multiple chat.sends; first one returns Phase 1 with Lumi-format runId; the next chat's UUID lifecycle (Phase 2) then pulls the orphan.
- **Symptom**: assistant turn shows two unrelated replies under one runId; off-by-one cascade for ~2 min until orphan TTL expires.
- **Fix**: replace skip with `RemovePendingChatTraceByRunID(payload.RunID)` so the matching entry is cleared instead of left as orphan.

### 2. sensing_input enter has no run_id
`flow.Start("sensing_input")` fires before `sendChat()` returns the run ID. The first event of a turn has no trace_id.
- **Mitigation**: Frontend assigns turn's runId from subsequent events (`sensing_input` exit has the ID).
- **Status**: Working. `isTurnStart` detects the event, `extractEventRunId` from later events fills in the ID.

### 3. Concurrent sensing events
Two sensing events arriving close together: turn B's `SetTrace` overwrites turn A's global trace. Turn A's lifecycle events may land with turn B's trace.
- **Mitigation**: Per-event runID means each `flow.Log` carries its own ID regardless of global state. The global trace is only used for the Telegram heuristic.
- **Status**: Mostly fixed. The `sensing_input enter` still has no per-event ID (pre-sendChat).

### 4. Double TTS
Both agent stream (`lifecycle_end` flush) and chat stream (`chat final assistant`) can send TTS for the same response.
- **Status**: Known bug, documented as TODO in handler.go. Fix: deduplicate with per-runID guard.

### 5. Server restarts every ~20s
WebSocket reconnects cause process-level restarts (seq counter resets). This is likely a separate stability issue, not a monitor bug.
- **Impact**: Trace lost mid-turn, events split across restarts.
- **Mitigation**: Per-event runID + frontend stitching handles most cases.

### 6. OpenClaw built-in `tts` tool bypasses LeLamp speaker (FIXED)
Agent called OpenClaw's built-in `tts` tool instead of responding with assistant text. OpenClaw generated audio server-side (`"Generated audio reply."`) but never routed it to the physical speaker (`/voice/speak` on LeLamp). Agent then returned `NO_REPLY`, so Lumi had no assistant text to flush → silent.
- **Root cause**: OpenClaw provides a built-in `tts` tool when `tools.profile = "full"`. The sensing SKILL.md instructed the agent to call `/voice/speak`, which the agent mapped to the built-in `tts` tool instead of using `curl` to LeLamp.
- **Fix**: (1) Deny OpenClaw built-in `tts` tool via `tools.deny: ["tts"]` in config (`service.go`). `tools.disabled` is NOT a valid OpenClaw key — use `tools.deny` (deny wins over `tools.profile`). (2) Intercept fallback in handler.go: if agent still calls `tts` tool, extract text and route to `SendToLeLampTTS()`. (3) Updated sensing SKILL.md and SOUL.md to instruct the agent to respond with plain text — Lumi's assistant-delta accumulation pipeline routes it to LeLamp TTS automatically.
- **Status**: Fixed in v0.0.138.

### 7. OpenClaw tool-call visibility gap (action without `tool_call`)
Observed on multiple Telegram turns: user asks for a device action (e.g. LED color change) and the lamp state/output confirms the action, but flow/debug logs contain only lifecycle + assistant/tts and no `tool_call` event.

- **Impact**: `TOOL` node can stay off even when an action appears to be executed.
- **Current status**: OpenClaw raw payload logging is enabled (`source: "openclaw_raw"`), but some runs still show no `stream:"tool"` payload.
- **Open question**: OpenClaw may be executing an internal path that does not emit tool stream, or action may be inferred from assistant text without explicit tool invocation.

## Compaction summary inspector

The OpenClaw agent session auto-compacts when context tokens cross ~80k. Every compaction writes a `type:"compaction"` record into `/root/.openclaw/agents/main/sessions/<sessionId>.jsonl` containing a `summary` string that is then prepended to every subsequent turn's prompt until the next compaction. Rules accidentally copied or generalized into that summary can override SKILL.md (the summary sits earlier in the prompt and is framed as "established context").

**UI:** Flow Monitor header exposes a `📋 Summary` button. Click → fetch + render modal showing the latest compaction record: timestamp, `tokensBefore`, `summaryChars`, `compactionCount`, `readFiles` fed into the compaction prompt, and the full `summary` text.

**Endpoint:** `GET /api/openclaw/compaction-latest?session=<key>` (default session key `agent:main:main`). Returns:

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

Use when Lumi cites rules that cannot be found in any `lumi/resources/openclaw-skills/**/SKILL.md` — the source is almost always the compaction summary, not the loaded skill. Handler: `lumi/server/openclaw/delivery/sse/handler_api_compaction.go`.

## Turns list vs downloaded log

| Source | Scope |
|--------|--------|
| **Turns list** (Monitor) | Built from the **last 10 000** `flow_events_*.jsonl` lines (`GET /openclaw/flow-events?last=10000`), then `groupIntoTurns` returns **all** turns (no cap). |
| **↓ Bundle** button | One click downloads **two**: (1) `GET /openclaw/flow-logs?last=10000` via `fetch` + blob save (`lumi_flow_YYYY-MM-DD_last10000.jsonl`) — **same tail** as the UI feed; (2) client JSON of `events[]` + grouped `turns[]` (`lumi_flow_ui_snapshot_*.json`). |
| **full day** link | `GET /openclaw/flow-logs` — entire day file; can be **longer** than the UI window, so Turns are **not** a reconstruction of the full file. |

Turns now show every turn derivable from the fetched events. Comparing server to UI should use **↓ Bundle** (or the same two artifacts manually: `flow-logs?last=10000` + UI snapshot JSON).

## Files

| File | Role |
|---|---|
| `lumi/lib/flow/flow.go` | Flow event emission, JSONL persistence, per-event runID API |
| `lumi/server/sensing/delivery/http/handler.go` | Sensing input → flow.Start/End with runID |
| `lumi/server/openclaw/delivery/sse/handler.go` | Agent events → flow.Log with payload.RunID, turn detection |
| `lumi/internal/openclaw/service.go` | sendChat returns idempotencyKey as runID |
| `lumi/web/src/pages/Monitor.tsx` | `groupIntoTurns`, `turnIO`, `extractNodeInfo`, `FlowDiagram` |

Vietnamese summary: `docs/vi/flow-monitor_vi.md`.
