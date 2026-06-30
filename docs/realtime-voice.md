# Realtime Voice Agent

Low-latency, speech-to-speech voice layer that runs **in parallel** with the
normal STT → agent pipeline. The realtime model handles casual conversation
directly (sub-second audio replies) and **delegates** anything that needs the
main agent (device control, skills, memory, real-time facts) back to the
OS-server flow.

Code lives in `os/hal/drivers/realtime/`; it is driven by
`os/hal/drivers/voice/voice_service.py`.

> **Source of truth:** this doc reflects the code. If they disagree, the code wins.

## Concept: handle vs. delegate

Every spoken turn is streamed to the realtime model *at the same time* as the
STT pipeline. At end-of-turn the model either:

- **Handles** the turn itself — chit-chat / quick answers — speaking back
  through TTS with no round-trip to the main agent, or
- **Delegates** by calling the `delegate_to_main` tool, which stops realtime
  output and forwards a one-line summary of the request to the OS server (→
  OpenClaw / Hermes) for the heavyweight work.

The `delegate_to_main` tool is registered automatically by the orchestrator
(`orchestrator.py`, `DELEGATE_TOOL`).

On a delegate call, `stream_output()` **breaks the turn immediately** after
yielding the `DelegateSignal` — it does *not* wait for the model's
`turn_complete`. The model has nothing more to say once it delegates, so
draining the rest of the turn would just block on the `receive()` timeout
(`HAL_REALTIME_RECV_QUEUE_TIMEOUT_S`) — the model stays silent for the full
window, adding that many seconds of latency before the main agent even sees the
request. The function result is already sent back to the model before the break;
the dangling open turn is cleared by the next turn's `flush_output()`.

## Emotion expression (fire-and-forget)

If the device declares the `expression` capability
(`DEVICE.md` → `expression: { routes: [emotion] }`), the orchestrator also
registers an `express_emotion` tool (`orchestrator.py`, `EMOTION_TOOL`).
Devices with no face (e.g. mic + speaker only) never get the tool, so the
realtime model can't set an emotion — the registration is gated end-to-end:
`server.py` (`"expression" in _profile.capabilities`) →
`VoiceService(enable_expression=…)` →
`RealtimeOrchestrator(enable_expression=…)`.

Unlike `delegate_to_main`, `express_emotion` is **fire-and-forget** and is the
one exception to the model's binary "tool OR speech" rule — the model calls it
*in parallel* with speaking. When `stream_output()` sees the call
(`_handle_emotion_call`), it:

1. calls the HAL emotion handler **in-process** (`_fire_emotion` →
   `routes/emotion.py` `express_emotion`) on a daemon thread — the realtime agent
   runs inside the HAL process, so there is no HTTP loopback / serialization. It
   runs parallel to the audio already streaming, so the face changes without
   blocking speech;
2. acknowledges the call with `FunctionCallResultInput(trigger_response=False)`,
   which records the result in history **without** spawning a second model
   response. For OpenAI this skips `response.create` (`openai_realtime.py`); for
   Gemini the tool response simply lets the turn continue. Net added latency to
   speech ≈ 0.

The model is told (`resources/system_prompt*.md`, "Expression Exception") to
never wait for, announce, or speak the emotion aloud. Note this is distinct from
the non-realtime path, where the agent emits a `[HW:/emotion:…]` text marker that
the Go layer parses and strips — the realtime path never uses text markers.

## Google Search grounding (Gemini only)

By default Gemini Live is given a built-in **Google Search** tool
(`HAL_GEMINI_GOOGLE_SEARCH`, default on; wired in `gemini_live.py` as a separate
`types.Tool(google_search=…)` alongside the function-declaration tools). This
lets the realtime model answer **public live-data** questions — weather, news,
sports, prices, "what time is sunset" — by grounding in-session and speaking the
result itself, instead of calling `delegate_to_main` and paying a full main-agent
round-trip. The Gemini system prompt (`system_prompt_gemini.md`) lists these
public lookups under *Direct Home Run* and routes only **account/private** live
data (the user's calendar, their smart-home device states, their messages) to
`delegate_to_main`.

Trade-offs:

- **Gemini only.** OpenAI Realtime has no equivalent built-in tool, so its prompt
  (`system_prompt_openai.md`) still delegates all external lookups.
- **Cost.** Grounding bills per grounded request on top of tokens, but only when
  Gemini actually decides to search. The prompt tells it to ground *only* for
  genuine fresh/public facts, not general knowledge it already holds. Net effect
  vs. before is mostly a **shift** of cost (and latency) off the main agent.
- **Read-only.** Grounding answers questions; it never performs actions. Music,
  hardware, memory writes, and skills still delegate.

## In-session vision — the `look` tool (Gemini only)

When the user asks about what the device **sees** ("what is this?", "what am I
holding?", "read this label", "what colour is this?"), the realtime model answers
in-session instead of delegating. The orchestrator registers a `look` tool
(`orchestrator.py`, `LOOK_TOOL`) and handles the call in `_handle_look_call`:

1. Grab the latest camera frame **in-process** (`_capture_frame` reads
   `app_state.camera_capture.last_frame` — no HTTP loopback, no servo-freeze),
   downscaled to `HAL_GEMINI_VISION_MAX_WIDTH` (default 768px) to bound image
   tokens.
2. Enqueue it as realtime **video input** (`ImageInput` → `send_realtime_input(video=…)`).
3. Send the tool result with `trigger_response=True` so Gemini **continues the
   same turn** and speaks the answer with the image now in context.

Because both sends go through the agent's single FIFO send queue, the frame
lands in context before generation resumes. Unlike `delegate_to_main`, `look`
does **not** break the turn — the model looks, then talks.

This replaces the slow path (delegate → main → skill lookup → `/camera/snapshot`
→ vision LLM, several seconds) with one in-session round-trip.

Gating (all three required, else visual questions fall back to delegation):

- **Capability:** a camera is present (`app_state.camera_capture` is set). This is
  the device's `vision` capability at runtime — `server.py` only creates
  `camera_capture` when DEVICE.md declares `vision`. The orchestrator reads that
  one signal (`_camera_present()`), so it's correct for every construction path.
- **Flag:** `HAL_GEMINI_VISION` / `realtime.gemini.vision` (default **on**).
- **Provider:** Gemini only (the image-inject → continue-turn flow is
  implemented + tested for Gemini Live; OpenAI keeps delegating). The
  Gemini system prompt (`system_prompt_gemini.md`) describes when to call `look`.

Cost: one frame per call (tool-triggered, **not** a video stream), so the added
tokens are marginal next to the turn's audio. A 768px frame is a few hundred
image tokens. To stop an over-eager model from re-billing images, `_handle_look_call`
sends **at most one image per turn** and **none within `HAL_GEMINI_VISION_MIN_INTERVAL_S`
(default 10s) of the last send** — repeat looks reuse the frame already in context.

**Frame handoff on delegate / timeout.** When a `look` turn ends up delegating or
falling back to the main agent (most importantly when Gemini times out *mid*-look),
the frame `look` already captured is handed to the main agent **by file path** so
it answers from that exact image instead of taking a fresh snapshot (faster, and
it answers about the moment the user pointed at). `_handle_look_call` persists the
frame to `_SNAPSHOT_DIR` and records it in `app_state.realtime_look_frame_path`;
`turn_dispatch._take_vision_handoff()` consumes it **once per turn** (strictly: a
handled turn that already used it clears it so a later delegate can't pick up a
stale image) and, when fresh (`HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S`, default 20s),
prepends a `[vision-image] <path>` line to the message sent to the agent. The
`camera` skill reads that path verbatim and skips `/camera/snapshot`. The handoff
carries the **path**, not the image bytes — HAL and the agent share the
filesystem, so a path avoids bloating the turn channel. If the timeout happens
*before* the frame is captured, there's nothing to hand off and the agent
snapshots normally.

## Providers

Two interchangeable backends, selected by `HAL_REALTIME_PROVIDER`
(`none` | `gemini` | `openai`):

| Provider | Class | Threading model | Default model | Sample rate |
|----------|-------|-----------------|---------------|-------------|
| Gemini Live | `voice_agent/gemini_live.py` `GeminiLiveAgent` | private asyncio loop on a `gemini-io` thread; send/recv threads submit coroutines via `run_coroutine_threadsafe` | `gemini-2.5-flash-native-audio-preview-12-2025` | 16000 Hz |
| OpenAI Realtime | `voice_agent/openai_realtime.py` `OpenAIRealtimeAgent` | fully synchronous; one `RealtimeConnection` shared by send/recv threads, serialized by a reentrant lock | `gpt-realtime-2` | 24000 Hz |

Gemini Live uses `google-genai`, but the SDK websocket keepalive is disabled
(`ping_interval=None`, `ping_timeout=None`) so the Python client behaves like the
browser raw-WS probe through the `campaign-api` proxy. The default Python
`websockets` 20 s ping loop can close idle sessions and make the next turn fail
with WS 1011. HAL also recycles Gemini synchronously before streaming audio when
the previous turn ended more than `HAL_GEMINI_PRE_TURN_RECYCLE_S` seconds ago, so
post-idle speech does not land on a proxy-dropped session.

Both subclass `voice_agent/base.py` `VoiceAgentBase`, which defines the
queue-based contract:

- **Two threads per agent**: `_send_loop` drains `_send_queue` → API;
  `_recv_loop` reads API → `_recv_queue`. Both reconnect on error.
- **Fail-fast on backend error** (both drivers): when `_recv_loop` hits a real
  error (Gemini Live: proxy `go_away`, quota / resource-exhausted, unexpected WS
  close — anything that is **not** a benign idle close `1000`; OpenAI: a Realtime
  API `error` event or dropped socket), it pushes a `TurnDoneEvent` immediately
  (`_fail_fast_turn`) so `receive()` unblocks now and the turn falls back to the
  main agent **without** waiting out the full `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S`.
  Benign idle closes still reconnect quietly (Gemini code `1000`; OpenAI ends the
  event iteration cleanly, never an error). Only fires while a turn is awaiting
  output (`_turn_done` clear); reconnect still runs in the background to heal the
  session for the next turn.
- **Non-blocking**: `append_audio()`, `commit_audio()`, `send()` (queue puts,
  gated on `available`).
- **Blocking**: `connect()`, `disconnect()`, `receive()` (a generator yielding
  `OutputBase` until a `TurnDoneEvent`, or until no event arrives within
  `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` — default 8 s — which ends the turn quietly
  so a silent/no-response turn falls back to the main agent without long dead-air).
- `available` ⇔ the websocket/session is connected (`_connected`).

### OpenAI connection safety

The OpenAI agent shares a single `RealtimeConnection` between its send and recv
threads. All connection writes, the connection swap during reconnect, and
teardown run under a reentrant lock (`_conn_lock`); the long blocking recv
iteration runs **outside** the lock on a connection snapshot so audio sends are
never starved mid-turn. Reconnect is idempotent (re-checks `_connected` under the
lock) and `_drop_connection()` only nulls a connection that is still current, so
the two threads can't tear down or rebuild each other's connection.

## Orchestrator

`orchestrator.py` `RealtimeOrchestrator` wraps a single agent session and is the
only surface `voice_service` talks to:

| Method | Purpose |
|--------|---------|
| `start()` / `stop()` | Build the agent from config, connect, summarize memory on shutdown |
| `append_audio(frame)` | Queue one mic frame (non-blocking) |
| `commit_audio()` | Signal end-of-utterance (non-blocking) |
| `stream_output()` | Yield `AudioOutput` / `TextOutput` / `FunctionCallOutput`, or a `DelegateSignal` (then stop) |
| `send_text(text)` | Inject context (turn context, TTS history) as a non-response user message. Gemini Live skips this to avoid SDK `clientContent`/audio turn collisions; OpenAI still accepts it. |
| `send_function_result(call_id, output)` | Return a tool result to the model |
| `save_turn(user, agent)` | Persist a turn to realtime memory |
| `available` / `sample_rate` | Readiness + provider audio rate |

## Context managers

The system prompt, device identity, device memory, and skills catalog are
assembled per agent gateway (`HAL_AGENT_GATEWAY`):

| Gateway | Class | Workspace |
|---------|-------|-----------|
| `openclaw` | `context_manager/openclaw.py` `OpenClawContextManager` | `HAL_OPENCLAW_WORKSPACE_DIR` (`/root/.openclaw/workspace`) |
| `hermes` | `context_manager/hermes.py` `HermesContextManager` | `HAL_HERMES_WORKSPACE_DIR` (`/root/.hermes`) |

`ContextManagerBase` (`context_manager/base.py`) handles prompt assembly
(`build_instructions`), turn persistence (`add_turn`), memory loading/trimming,
and summarization; subclasses implement `load_device_context`,
`load_device_memory`, `load_skills_catalog`, and `summarize_device_memory`.
Base prompts live in `resources/` (`system_prompt.md` plus per-provider
`system_prompt_openai.md` / `system_prompt_gemini.md`).

### Memory & summarization

Realtime turns are appended to a JSONL log (`HAL_REALTIME_MEMORY_PATH`, default
`<workspace>/realtime/memory.jsonl`), trimmed to `HAL_REALTIME_MAX_MEMORY_ENTRIES`
(keeping `HAL_REALTIME_MEMORY_TRIM_KEEP`). `RealtimeSummarizer` (`summarizer.py`)
condenses device + realtime memory via the **Anthropic Messages API**
(`HAL_REALTIME_SUMMARIZER_MODEL`, default `claude-haiku-4-5-20251001`).
Summarization runs at `start()` (catch-up) and `stop()` (flush). The `start()`
catch-up runs in a **background thread** (after `connect()`), so the Anthropic
call never blocks the session from becoming `available` — otherwise an early
turn ("hello") right after a restart would leak to the main agent.

## Turn flow (in `voice_service.py`)

1. **Construct + start.** `RealtimeOrchestrator(gateway=AGENT_GATEWAY)` is built;
   `start()` runs in a daemon thread (`realtime-start`) when `HAL_REALTIME_ENABLED`.
   TTS `on_speak_end` is hooked to feed spoken text back as `[TTS HISTORY]`,
   but **only when that speech opted in** (`TTSService.realtime_feedback`, set by
   the `realtime_feedback` flag on `/voice/speak[-queue]`). Only the agentic
   runtime's actual reply opts in — os-server sends it via `hal.SpeakReply` /
   `hal.SpeakQueueReply` (which `SendToHALTTS` / `SendToHALTTSQueue` use).
   Hardcoded TTS (dead-air fillers, ambient mumble, backchannel, reconnect /
   health notices, local chitchat) goes through plain `hal.Speak` and is **never**
   fed back — otherwise the model would echo lines it never generated.
2. **Prime context.** After STT connects but before any buffered mic frame is sent
   to realtime, `[TURN CONTEXT]` (time, reply-language reminder, current user) is
   offered as non-response text. Gemini Live intentionally drops this injection
   because repeated SDK `clientContent(turn_complete=False)` messages can collide
   with later audio turns and close with WS 1011; the browser probe sends no such
   context messages.
3. **Stream.** While the STT session is open, each mic frame is also resampled to
   the provider rate and sent via `append_audio()` (parallel, non-blocking), and
   buffered in `rt_audio_buffer`.
4. **Commit.** At session end, if enabled + `available` + audio buffered,
   `commit_audio()` fires.
5. **Consume.** `for output in stream_output()`:
   - `TextOutput` → sentences are flushed to TTS (`speak` / `speak_queue`).
   - `DelegateSignal` → stop; forward `[voice-instruction] …` + transcript to the
     OS server with the original `event_type`.
   - Otherwise the turn was handled locally → the OS server is told
     `voice_agent_handled` (so OpenClaw replies `NO_REPLY` and skips dead-air
     filler), and the turn is saved to realtime memory.

## Configuration

The realtime agent is configured from the **`realtime` block in the device's
`config.json`** (operator-facing knobs), with HAL's `HAL_*` environment variables
as a dev override and built-in defaults as the floor. Precedence per knob:

```
HAL_* env var  >  config.json "realtime" block  >  built-in default
```

os-server **seeds** the block into `config.json` on first start — and on upgrade
when it's absent — so the file always carries an editable realtime config. HAL
reads it directly (same as `llm_api_key` / `stt_language`), no push down. Because
HAL reads `config.json` at import, a config change needs a **HAL restart** to take
effect. A live edit triggers that restart immediately (`RePushRealtimeConfig` /
`RePushVoiceConfig` in `internal/device/service.go`).

**Restart only when the config changed.** os-server does *not* restart HAL on
every os-server restart — that would needlessly drop the voice pipeline. Instead
it hashes `config.json` and stores the hash in `config/.hal_config_hash` whenever
it (re)starts HAL. On boot (`handleSetUpCompleteChange` in `server/config_watch.go`)
it restarts HAL only when the current hash differs from that snapshot — i.e. the
config actually changed while os-server was down (fresh setup, OTA config swap, an
edit during downtime), or no snapshot exists yet (first boot). A plain os-server
restart with unchanged config leaves the already-running HAL untouched. If HAL is
genuinely down, `hal.service` (`Restart=always`, `RestartSec=5`) brings it back
independently, so skipping the restart is safe. The `RePush*` paths refresh the
snapshot after they restart HAL, so a live change followed by an os-server restart
doesn't double-restart. Hashing the whole file (rather than the HAL-read subset)
keeps the signal self-maintaining as HAL's read set evolves; the only cost is one
spurious HAL restart on the next boot after an os-server-only field changes.

### `config.json` `realtime` block

Modelled in Go at `os/services/server/config/realtime.go`; read in HAL at
`os/hal/config.py`. Shared fields sit at the top; per-provider knobs live in
`gemini` / `openai` sub-objects, with `provider` selecting the active one
(`none` or absent → realtime off). Empty `api_key` / `base_url` fall back to
`llm_api_key` / `llm_base_url`.

> **Leave `base_url` blank unless you have a non-proxy endpoint.** When empty, HAL
> derives `<llm_base_url>/ws/gemini` (or `/ws/openai`) — the WS suffix the
> `campaign-api` proxy routes on. A `base_url` set to the bare `llm_base_url`
> (no `/ws/...`) is handed verbatim to the provider SDK and **404s at the Live
> handshake**. The web Settings "Base URL" field is therefore display-bound to the
> *explicit override only* (`RealtimeBaseURLOverride`, not the resolved value), so
> "leave blank to derive" stays blank and a save never re-persists the bare URL.

```json
"realtime": {
  "enabled": true,
  "provider": "gemini",
  "gemini": { "model": "gemini-3.1-flash-live-preview", "voice": "Kore", "thinking_level": "MINIMAL" },
  "openai": { "model": "gpt-realtime-2", "voice": "alloy", "reasoning_effort": "minimal" }
}
```

The reasoning knobs (`thinking_level` / `reasoning_effort`) default to the
**cheapest** tier (`MINIMAL` / `minimal`), not the providers' max — raise them
explicitly for deeper reasoning. Knobs NOT in the block (turn detection, session
resumption, memory, summarizer) stay env/default-only.

### Environment variables (`os/hal/config.py`)

Each knob's `HAL_*` env var overrides the block (and is the dev-box path):

| Variable | Default | Notes |
|----------|---------|-------|
| `HAL_REALTIME_ENABLED` | `true` | Master gate for the realtime pipeline |
| `HAL_REALTIME_PROVIDER` | `gemini` | `none` \| `gemini` \| `openai` |
| `HAL_REALTIME_TURN_DETECTION` | `off` | `server_vad` \| `semantic_vad` \| `off` (Gemini: off = manual activity detection) |
| `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` | `8.0` | Max seconds `receive()` waits for the next output event before ending a silent turn (fallback to main agent) |
| `HAL_REALTIME_REQUIRE_TRANSCRIPT` | `true` | Never commit an empty-STT turn to the model. Real speech that nova-3 missed (short utterances) is voiced and passes the VAD/Silero guards, so committing its raw audio makes the model invent a reply to silence (a generic greeting, often with a name nobody said). When `true`, any empty-STT turn is dropped regardless of duration/voicing — silence beats a wrong reply. Set `false` to fall back to the Silero-gated audio-only path below. |
| `HAL_REALTIME_MIN_COMMIT_DURATION_S` | `0.8` | Sessions shorter than this with no STT transcript are treated as VAD noise and not committed to the model. Only consulted when `HAL_REALTIME_REQUIRE_TRANSCRIPT=false`. |
| `HAL_REALTIME_SESSION_IDLE_RESET_S` | `240` | Cost control: when a turn arrives after this many seconds of silence, recycle (rebuild) the session **after** that turn so the next turn drops the per-turn context the provider re-bills on a long-lived session. A post-pause turn is effectively a new conversation; long-term continuity survives via the reloaded `summary.md`. `0` disables. Reuses the zombie-recovery rebuild path. |
| `HAL_GEMINI_SESSION_RESUMPTION` | `false` | Resume the same Gemini session across reconnects. OFF by default — the `campaign-api` proxy doesn't forward the resumption handshake, so resuming through it yields a zombie session (cold reconnects work). Enable only against an endpoint that supports it. |
| `HAL_GEMINI_PRE_TURN_RECYCLE_S` | `15` | Gemini transport guard: when a new spoken turn starts after this much idle time, rebuild the Gemini session **before** streaming pre-roll/audio so the turn does not hit a proxy/SDK idle-dead socket. `0` disables. |
| `HAL_AGENT_GATEWAY` | `openclaw` | Selects the context manager (also from `agent_runtime` in config.json) |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | Gemini key; falls back to `llm_api_key` |
| `HAL_GEMINI_LIVE_MODEL` | `gemini-2.5-flash-native-audio-preview-12-2025` | |
| `HAL_GEMINI_LIVE_VOICE` | `Kore` | |
| `HAL_GEMINI_LIVE_BASE_URL` | `<llm_base_url>/ws/gemini` | |
| `HAL_GEMINI_THINKING_LEVEL` | `MINIMAL` | `MINIMAL` \| `LOW` \| `MEDIUM` \| `HIGH` — cost-lean default (was `HIGH`) |
| `HAL_GEMINI_GOOGLE_SEARCH` | `true` | Google Search grounding (Gemini only). Lets the realtime model answer public live-data questions (weather, news, lookups) in-session instead of delegating. Bills per grounded request on top of tokens; fires only when Gemini decides to search. Also settable via `realtime.gemini.google_search` in config.json. |
| `HAL_GEMINI_VISION` | `true` | In-session `look` tool (Gemini only). Lets the realtime model capture one camera frame and answer visual questions ("what is this?") in-session instead of delegating. Default on; only registered when the device also has the `vision` capability. Also settable via `realtime.gemini.vision` in config.json. |
| `HAL_GEMINI_VISION_MAX_WIDTH` | `768` | Max width (px) the captured frame is downscaled to before sending — bounds image tokens. |
| `HAL_GEMINI_VISION_MIN_INTERVAL_S` | `10` | Cost guard: minimum seconds between two image **sends**. Repeat `look` calls within this window (or a second call in the same turn) reuse the frame already in context instead of sending a new one. `0` = always send fresh. |
| `HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S` | `20` | Max age of a `look` frame still handed off (by path) to the main agent on a delegate/timeout fallback so it reuses the image instead of re-snapshotting. `0` disables the age guard (frame is still cleared per-turn). |
| `OPENAI_API_KEY` | — | OpenAI key; falls back to `llm_api_key` |
| `HAL_OPENAI_REALTIME_MODEL` | `gpt-realtime-2` | |
| `HAL_OPENAI_REALTIME_VOICE` | `alloy` | |
| `HAL_OPENAI_REALTIME_BASE_URL` | `<llm_base_url>/ws/openai` | |
| `HAL_OPENAI_REASONING_EFFORT` | `minimal` | `minimal` \| `low` \| `medium` \| `high` \| `xhigh` — cost-lean default (was `xhigh`) |
| `HAL_REALTIME_MEMORY_PATH` | `<workspace>/realtime/memory.jsonl` | |
| `HAL_REALTIME_MAX_MEMORY_ENTRIES` / `_TRIM_KEEP` | `1000` / `500` | |
| `HAL_REALTIME_SUMMARIZER_ENABLED` | `true` | |
| `HAL_REALTIME_SUMMARIZER_MODEL` | `claude-haiku-4-5-20251001` | Anthropic Messages API |

## Code map

| File | Role |
|------|------|
| `orchestrator.py` | Session lifecycle, `delegate_to_main` + `express_emotion` + `look` tools, turn streaming |
| `voice_agent/base.py` | Abstract agent: two-thread queue contract, `receive()` |
| `voice_agent/gemini_live.py` | Gemini Live provider (asyncio IO loop) |
| `voice_agent/openai_realtime.py` | OpenAI Realtime provider (sync, lock-serialized connection) |
| `context_manager/{base,openclaw,hermes}.py` | Prompt + memory + skills assembly per gateway |
| `summarizer.py` | Anthropic-based memory summarizer |
| `config.py` | Provider config models (`GeminiConfig`, `OpenAIConfig`) |
| `models/`, `enums/` | Input/output/event types, provider + gateway enums |
| `resources/` | System prompts (shared + per-provider) |
| `../voice/voice_service.py` | Integration: streams mic audio, consumes output, routes delegate/handled |
