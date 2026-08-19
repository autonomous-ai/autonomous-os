# Realtime Voice Agent

Low-latency, speech-to-speech voice layer that runs **in parallel** with the
normal STT → agent pipeline. The realtime model handles casual conversation
directly (sub-second audio replies) and **delegates** anything that needs the
main agent (device control, skills, memory, real-time facts) back to the
OS-server flow.

Code lives in `hal/realtime/`; it is driven by
`hal/drivers/voice/voice_service.py`.

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

Gemini can similarly emit `generation_complete` before `turn_complete`: the
latter is delayed while Gemini assumes the client is playing its audio in real
time. HAL plays the generated response itself, so it ends the consumer turn on
`generation_complete` and releases the next manual-VAD commit immediately.
This avoids an otherwise unnecessary silent-watchdog delay after the reply;
any late `turn_complete` is discarded before the next turn.

The gate itself is `wakeword` in `config.json` (Settings → "Require a wake word
before handling speech"). A device being set up for the first time takes its
initial value from the body's `voice.wakeword` in `robots/<type>/ROBOT.md` —
lamp declares `true`; a body that declares nothing stays always-listening. A
device provisioned before the key existed keeps always-listening across
upgrades: os-server only adopts the ROBOT.md default while `config.json` has no
`wakeword` key at all.

Accepted phrases are `hello|hey|hi|alo|okay|ok|wake up` + `autonomous`, the
device type (`lamp`), or the agent name from IDENTITY.md — HAL resolves the
device type from the `DEVICE_TYPE` env first, then `config.json`, so the
runtime list matches the one Settings advertises.

A mic session is a continuous stretch of speech, not a single sentence, so the
match is **per sentence**: `starts_with_wake_word()`
(`hal/drivers/voice/_internal/speaker_decorate.py`) splits the transcript on
`.` `!` `?` and accepts a wake phrase at the **start or the end of any
sentence**. Mid-sentence occurrences are still rejected — a device name in the
middle of a sentence is people talking *about* the device ("this lamp is
nice"), and opening the gate there would make it barge into someone else's
conversation. The end-of-sentence position is accepted because calling the name
last is a natural vocative ("what time is it, hey lamp?"). Without the
per-sentence rule a turn like "What was the score of the Vietnam versus
Malaysia match? Hi lamp, can you hear me?" was dropped whole and the user just
heard silence. The function keeps its `starts_with_wake_word` name because
every caller reads it as "was this turn addressed to me?".

The final-result confirmation runs on the **assembled** transcript, which still
carries its punctuation. `merge_stt_hypothesis()` keeps only `\w+` tokens and
therefore strips sentence boundaries, which would collapse the whole turn into
one sentence and retract a gate that a partial had correctly opened. So before
dropping a turn whose partial armed the gate, the capture loop re-checks
`starts_with_wake_word(combined)` on the real transcript: a match sets
`wake_word_confirmed` and logs `Wake-word confirmed on assembled transcript`,
and only a genuine mismatch drops the turn.

All three names are sent to STT as boost terms (`_stt_boost_terms`), because a
mis-heard name silently drops the whole turn — "hi lamp" transcribed as "hi
lance" never arms the gate. Flux takes them as repeated `keyterm` parameters
with no weights; nova-3 uses `keyterm` too; older nova models use `keywords`
with the `:3` intensifier.

Every STT-final-confirmed wake-word turn reaches dispatch. It opens a 20-second
follow-up focus window (reset after every authorized turn), so the next spoken
turn can omit the wake phrase and is sent as `voice_followup`. A follow-up has
the same user priority as `voice_command`, but remains separately observable.
When realtime already spoke, dispatch sends a `voice_agent_handled`
synchronization event so the main agent records the exchange but stays silent;
unavailable, failed, timed-out, or delegated realtime takes the normal
main-agent path. This also consumes a one-turn vision handoff, so a temporary
Gemini failure cannot drop a voice command or leak a frame into the next turn.

### Silero guards the silence clock (end of turn)

A mic session ends when the audio stays below the RMS threshold for
`SILENCE_TIMEOUT_S`. RMS alone is not enough in a noisy room: room noise sits
above `RMS_THRESHOLD`, so every frame refreshed the clock, the turn ran to
`MAX_SESSION_DURATION_S`, and mostly-noise audio went to STT — the 18/08/2026
observation was 8–25 second sessions coming back with `transcript='(empty)'`.
Energy VAD misses roughly half of the real speech frames in that environment,
and production voice stacks (Pipecat, LiveKit, Deepgram) all put a neural VAD
on this decision.

RMS stays as the cheap first gate, but the silence clock is only refreshed once
Silero also confirms speech. Silero runs per **window**
(`SILENCE_VAD_WINDOW_FRAMES`), not per frame: it costs ~20 ms/frame on ARM and
its LSTM needs more than one 64 ms frame to settle. It uses its **own** Silero
instance — a third one, alongside the entry gate and the realtime noise guard —
so the other paths' LSTM state stays clean, and it resets that state at the
start of every session. It fails open: a model error counts as speech, so the
device never cuts anyone off.

`robots/lamp/rootfs/opt/hal/.env` lowers `HAL_MAX_SESSION_DURATION_S` to `20`
(the code default stays `30`); that ceiling is only reached when the silence
clock never expires, and a real speaker always pauses longer than
`SILENCE_TIMEOUT` within 20 seconds. The same file previously wrote
`WAKEWORD_FOLLOWUP_TIMEOUT_S=60` without the `HAL_` prefix, so it did nothing
and the device ran the 20 s default; the key is now
`HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S=60`.

If the **initial** provider connection fails during HAL startup, the
orchestrator creates fresh sessions in a background retry loop (an immediate
fresh attempt, then 2s exponential backoff capped at 60s). This is separate from provider send/receive reconnects,
which do not exist until the first `connect()` succeeds. No HAL restart or new
audio is required; voice turns keep using the main-agent fallback until the
connection recovers.

## Emotion expression (fire-and-forget)

If the device declares the `expression` capability
(`ROBOT.md` → `expression: { routes: [emotion] }`), the orchestrator also
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
   response. For OpenAI and Qwen this skips `response.create`
   (`openai_realtime.py` / `qwen_realtime.py`); for
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

- **Gemini only.** OpenAI Realtime and Qwen Omni Realtime have no equivalent
  built-in tool, so their prompts (`system_prompt_openai.md` /
  `system_prompt_qwen.md`) still delegate all external lookups.
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

1. Grab a **sharp** camera frame **in-process** (`_capture_frame` calls
   `capture_still` — no HTTP loopback; servos are frozen (animation loop +
   tracker worker both honor the flag) and the frame is only accepted once its
   capture timestamp is ≥ 0.3s after the last servo bus write, so motion blur
   can't reach the model; zero added latency when the servos are already still
   or the device has none), downscaled to `HAL_GEMINI_VISION_MAX_WIDTH`
   (default 768px) to bound image tokens.
2. Enqueue it as realtime **video input** (`ImageInput` → `send_realtime_input(video=…)`),
   then **replay the turn**: the Live API queues a frame sent mid-turn for the
   NEXT turn (device-proven: the tool-ack → continue-turn flow answered every
   look from the *previous* look's image — a one-image lag no ack delay fixes),
   so instead of acking the tool call, the orchestrator yields `LookReplaySignal`
   and `run_realtime_turn` re-appends the turn's audio and commits again on the
   SAME session. The queued frame joins the replayed turn.
3. The replayed turn re-triggers `look`, which hits the reuse guard
   (`VISION_MIN_INTERVAL_S`) and is acked with `trigger_response=True` — the
   model answers from the frame that is now genuinely in context.

Replay support plumbing: `receive()` swallows ONE stale `turn_complete` (the
cancelled turn's, which lands after the replay commit and would otherwise end
the replayed turn empty — `skip_next_turn_done()`); a pending idle/turn-cap
session recycle is deferred while a replay is pending (a rebuild would orphan
the just-sent image); and any session rebuild resets the look reuse guard
(images live in the session — a fresh session has none). Cost: the question's
audio is billed twice on look turns; the image once.

This replaces the slow path (delegate → main → skill lookup → `/camera/snapshot`
→ vision LLM, several seconds) with one in-session round-trip.

Gating (all three required, else visual questions fall back to delegation):

- **Capability:** a camera is present (`app_state.camera_capture` is set). This is
  the device's `vision` capability at runtime — `server.py` only creates
  `camera_capture` when ROBOT.md declares `vision`. The orchestrator reads that
  one signal (`_camera_present()`), so it's correct for every construction path.
- **Flag:** `HAL_GEMINI_VISION` / `realtime.gemini.vision` (default **on**).
- **Provider:** Gemini only (the image-inject → continue-turn flow is
  implemented + tested for Gemini Live; OpenAI and Qwen keep delegating —
  Qwen's session is text + audio only). The
  Gemini system prompt (`system_prompt_gemini.md`) describes when to call `look`.

Cost: one frame per call (tool-triggered, **not** a video stream), so the added
tokens are marginal next to the turn's audio. A 768px frame is a few hundred
image tokens. To stop an over-eager model from re-billing images, `_handle_look_call`
sends **at most one image per turn** and **none within `HAL_GEMINI_VISION_MIN_INTERVAL_S`
(default 10s) of the last send** — repeat looks reuse the frame already in context.

**Frame handoff on delegate / timeout.** When a `look` turn ends up delegating or
falling back to the main agent (most importantly when Gemini times out *mid*-look),
the frame `look` already captured is handed to the main agent so it answers from
that exact image instead of taking a fresh snapshot (faster, and it answers about
the moment the user pointed at). `_handle_look_call` persists the frame to
`_SNAPSHOT_DIR` and records it in `app_state.realtime_look_frame_path`;
`turn_dispatch._take_vision_handoff()` consumes it **once per turn** (strictly: a
handled turn that already used it clears it so a later delegate can't pick up a
stale image) and, when fresh (`HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S`, default 20s),
prepends a `[vision-image] <path>` hint line to the message and ships the frame
as base64 in the sensing POST's `image` field. What os-server
then does with the image is decided by the **describe-first gate** in
`system/vision` (see `server/sensing/delivery/http/handler.go`): when the
active main model does NOT declare image input in the model catalog (the
Auto-AI case — a raw attachment 404s at the smart-agent-router with "No
endpoints found that support image input"), the frame is described by the
catalog's `default_image_model` (qwen — the same model openclaw's `imageModel`
uses for Telegram photos) and the agent receives an `[image description] …`
text line instead — and the `[vision-image]` hint is rewritten to drop the
file path, plus the snapshot file itself is deleted (best-effort). Neither
may survive alongside a description: the snapshot lives inside the agent's
media allow-list, so any path the agent gets hold of — the hint, an old hint
in session history, an `ls` of the dir — can be `read` into an image block
that sticks in the session history and 404s every later turn the router
sends to a text-only model (even fully-text turns). The describe call gets
two attempts (20s + 15s, 35s total — a hung upstream request is retried on a
fresh connection); if both fail the image is **dropped**, the snapshot file
still deleted, and the hint rewritten to have the agent tell the user it
couldn't see the photo — never sent as a raw attachment, because when the
router lands on a text-only model that attachment poisons the whole session,
which costs far more than one degraded turn. When the catalog says
the model takes images, the raw attachment is forwarded directly and the
hint keeps the path. The gate re-reads the catalog every 30 min,
so a backend catalog flip migrates devices automatically. The same gate covers
web-monitor-chat image uploads — both image sources converge on this one
handler. The `camera` skill instructs the agent to answer from the
description/attachment and skip `/camera/snapshot`. If the timeout happens
*before* the frame is captured, there's nothing to hand off and the agent
snapshots normally.

## Providers

Three interchangeable backends, selected by `HAL_REALTIME_PROVIDER` /
`realtime.provider` (`none` | `gemini` | `openai` | `qwen`):

| Provider | Class | Threading model | Default model | Sample rate |
|----------|-------|-----------------|---------------|-------------|
| Gemini Live | `voice_agent/gemini_live.py` `GeminiLiveAgent` | private asyncio loop on a `gemini-io` thread; send/recv threads submit coroutines via `run_coroutine_threadsafe` | `gemini-2.5-flash-native-audio-preview-12-2025` | 16000 Hz |
| OpenAI Realtime | `voice_agent/openai_realtime.py` `OpenAIRealtimeAgent` | fully synchronous; one `RealtimeConnection` shared by send/recv threads, serialized by a reentrant lock | `gpt-realtime-2` | 24000 Hz |
| Qwen Omni Realtime | `voice_agent/qwen_realtime.py` `QwenRealtimeAgent` | fully synchronous; raw `websockets.sync.client` socket shared by send/recv threads, reusing the openai_realtime thread/queue skeleton | `qwen3.5-omni-plus-realtime` | 16000 Hz in / 24000 Hz out |

Gemini Live uses `google-genai` and keeps its private asyncio loop owned by its
`gemini-io` thread. Teardown first closes/cancels the provider receive task,
then joins workers; a failed handshake rolls back that loop/thread immediately.
This prevents a stalled receive from surviving a session rebuild. For the
native-audio family, HAL sends a 20 s websocket ping but sets no ping timeout:
outbound traffic keeps the proxy path alive without treating its missing pong as
a client-side failure. HAL also recycles Gemini synchronously before streaming audio when
the previous turn ended more than `HAL_GEMINI_PRE_TURN_RECYCLE_S` seconds ago, so
post-idle speech does not land on a proxy-dropped session.

All providers treat teardown as terminal: once `disconnect()` sets the stop
signal, send/receive workers neither reconnect nor emit transport-failure logs
while their closed socket unwinds.

Qwen Omni Realtime (Alibaba DashScope / Model Studio) speaks the **OpenAI
Realtime beta event schema** (`session.update`, `input_audio_buffer.append` /
`commit`, `response.create`, `response.audio.delta`,
`response.audio_transcript.delta`, `response.done`) over DashScope's own WS
path `wss://<workspace-host>/api-ws/v1/realtime?model=...` with
`Authorization: Bearer <key>`. The OpenAI python SDK cannot be reused (it
emits/parses the GA schema), so `qwen_realtime.py` drives the socket directly
with `websockets.sync.client`. Turn flow is the same manual-commit pattern (HAL
does its own VAD: append → commit → `response.create`); `response.create` MUST
carry an explicit `response.modalities ["text","audio"]` or the server answers
text-only (verified live 2026-07-06). Audio is 16 kHz mono pcm16 base64 in,
24 kHz mono pcm16 out. Built-in web search (3.5 models) is enabled via session
`enable_search: true` (knob `realtime.qwen.search` / `HAL_QWEN_SEARCH`, default
on) — the qwen twin of Gemini's Google Search grounding. DashScope constraint:
search ("agent mode") REJECTS function tools in the same session, so with
search on, delegation runs over a text-marker protocol instead — the agent
appends a `[TOOL PROTOCOL]` suffix to the instructions, the model replies
exactly `[DELEGATE] <message>`, and the recv loop swallows that transcript and
synthesizes the same `delegate_to_main` FunctionCallOutput a real tool call
would produce (the orchestrator can't tell the difference; `express_emotion`
is unavailable in this mode). With search off, function tools
(`delegate_to_main`, `express_emotion`) are passed in `session.update` (beta
flat format) and `response.function_call_arguments.done` is handled. The default model is
`qwen3.5-omni-plus-realtime`: the legacy `qwen-omni-turbo-realtime` never fires
function calls and ignores `[TURN CONTEXT]` (device-tested 2026-07-06), which
breaks the delegate flow entirely. Voices: `Ethan` (default) and `Serena` on
3.5-plus; `Cherry`/`Chelsie` are turbo-only (a wrong pairing fails with
`InvalidParameter` on the first response). There is **no reasoning/thinking
knob** (the web Settings page hides the Reasoning selector for qwen). Capability-wise qwen has
**no Google Search grounding and no in-session vision/`look`** (text + audio
only) — live-data and visual questions are delegated to the main agent.
Per-turn token/cost lines go to their own log file `qwen_usage.log` (logger
`hal.realtime.usage.qwen`, the twin of `gemini_usage.log`); the `_QWEN_RATES`
table in `qwen_realtime.py` holds $0.27/1M input, $1.07/1M output (Model Studio
intl publishes a single blended rate; the table keeps per-modality keys so a
console-verified split can be dropped in). Audio ≈ 25 tokens/second in both
directions (verified: 5.1 s audio out = 128 tokens); the usage payload carries
`input_tokens`/`output_tokens`, `input_tokens_details`/`output_tokens_details`
(`text_tokens`, `audio_tokens`) and a top-level `cached_tokens`.

All subclass `voice_agent/base.py` `VoiceAgentBase`, which defines the
queue-based contract:

- **Two threads per agent**: `_send_loop` drains `_send_queue` → API;
  `_recv_loop` reads API → `_recv_queue`. Both reconnect on error.
- **Fail-fast on backend error** (all drivers): when `_recv_loop` hits a real
  error (Gemini Live: proxy `go_away`, quota / resource-exhausted, unexpected WS
  close — anything that is **not** a benign idle close `1000`; OpenAI/Qwen: a
  Realtime API `error` event or dropped socket), it pushes a `TurnDoneEvent` immediately
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

## Pricing & usage logs

Every turn writes one token/cost line to a per-provider log under
`/var/log/hal/` (rotating, 5 MB × 3): `gemini_usage.log` (logger
`hal.realtime.usage`) and `qwen_usage.log` (logger `hal.realtime.usage.qwen`).
OpenAI logs a plain usage line into `server.log` (`[realtime] OpenAI usage`),
no cost estimate. The line carries per-modality token counts **and** an
estimated USD cost, so a wrong rate can always be re-derived later from the
logged counts.

Rate tables live in code, keyed `(direction, modality)` in USD per 1M tokens —
`_GEMINI_RATES` in `voice_agent/gemini_live.py`, `_QWEN_RATES` in
`voice_agent/qwen_realtime.py`. Unknown models fall back to the most expensive
table (cost ceiling, never an under-report).

| Model | text in | audio in | text out | audio out | audio↔token | Source |
|---|---|---|---|---|---|---|
| `gemini-2.5-flash-native-audio` | $0.50 | $3.00 | $2.00 | $12.00 | 25 tok/s | ai.google.dev pricing (verified 2026-06-29) |
| `gemini-3.1-flash-live` | $0.75 | $3.00 | $4.50 | $12.00 | 25 tok/s | ai.google.dev pricing (verified 2026-06-29) |
| `qwen-omni-turbo-realtime` | $0.27 | $4.44 | $8.89* | $8.89* | 25 tok/s in+out | consume-detail bill CSV (verified 2026-07-06); *audio-modality output bills text+audio together (`multi_output_token`); text-only responses bill $1.07 (`purein_text_output`) |
| `qwen3.5-omni-flash-realtime` | $0.27 | $4.44 | $8.89* | $8.89* | ~7 tok/s in, ~12.5 tok/s out | bill CSV (verified 2026-07-06): flash bills under the SAME cheap line items as turbo, incl. search-enabled sessions — dominant text-in cost ~2.8x cheaper than Gemini 3.1. Search +$0.01/request |
| `qwen3.5-omni-plus-realtime` | $2.10 | $16.50 | $62.00* | $62.00* | ~7 tok/s in, ~12.5 tok/s out | consume-detail bill CSV (verified 2026-07-06); *one `omni_audio_output_token` line item covers the response's text+audio. Web search bills $0.01 per search request on top |

Cost anatomy is the same on every provider: `in_text` dominates (the ~7-10k
token system prompt plus accumulated session context is re-billed every turn
and grows until a session recycle — see `HAL_REALTIME_SESSION_IDLE_RESET_S` /
`HAL_REALTIME_SESSION_MAX_TURNS`); audio tokens are comparatively marginal.
Gemini additionally bills Google Search per grounded request on top of tokens.

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
| `rebuilding` / `wait_until_available()` | Observe and briefly wait for an already-running replacement session without starting another rebuild |

## Context managers

The system prompt, device identity, device memory, and skills catalog are
assembled per agent gateway (`HAL_AGENT_GATEWAY`):

| Gateway | Class | Workspace |
|---------|-------|-----------|
| `openclaw` | `context_manager/openclaw.py` `OpenClawContextManager` | `HAL_OPENCLAW_WORKSPACE_DIR` (`/root/.openclaw/workspace`) |
| `hermes` | `context_manager/hermes.py` `HermesContextManager` | `HAL_HERMES_WORKSPACE_DIR` (`/root/.hermes`) |
| `picoclaw` | `OpenClawContextManager` (same layout) | `HAL_PICOCLAW_WORKSPACE_DIR` (`/root/.picoclaw/workspace`) |
| `codex` | `OpenClawContextManager` (same layout) | `HAL_CODEX_WORKSPACE_DIR` (`/root/.codex/workspace`) |
| `claudecode` | `context_manager/claudecode.py` `ClaudeCodeContextManager` — OpenClaw layout except skills, read from `.claude/skills/` (native claude CLI dir) | `HAL_CLAUDECODE_WORKSPACE_DIR` (`/root/.claudecode/workspace`) |
| `opencode` | `OpenClawContextManager` (same layout; like codex its skills live in a non-workspace dir `~/.config/opencode/skills`, so the workspace skills catalog is empty — identity + memory load correctly) | `HAL_OPENCODE_WORKSPACE_DIR` (`/root/.opencode/workspace`) |

`ContextManagerBase` (`context_manager/base.py`) handles prompt assembly
(`build_instructions`), turn persistence (`add_turn`), memory loading/trimming,
and summarization; subclasses implement `load_device_context`,
`load_device_memory`, `load_skills_catalog`, and `summarize_device_memory`.
Base prompts live in `resources/` (`system_prompt.md` plus per-provider
`system_prompt_openai.md` / `system_prompt_gemini.md` / `system_prompt_qwen.md`,
registered in the context manager's `PROVIDER_PROMPT_PATHS` map).

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
2. **Stream.** While the STT session is open, each mic frame is also resampled to
   the provider rate and sent via `append_audio()` (parallel, non-blocking), and
   buffered in `rt_audio_buffer`.
   When optional STT keepalive is enabled, a pre-connected STT socket that closes
   normally (WS 1000) at speech start is replaced before streaming continues and
   the complete pre-roll is replayed once on the fresh socket. This preserves the
   opening words; a recovered normal close is a warning, not an error.
   Gemini Manual VAD cannot cancel an already-streamed activity. Therefore an
   empty-STT/noise turn starts a clean replacement session rather than letting
   its noise contaminate the next user turn. That reconnect runs in the
   background: if the user speaks immediately, HAL keeps the entire next turn
   locally, then sends it once in order when the replacement session is ready.
   A slow/failed reconnect falls back to the main agent with the STT transcript;
   it never drops the opening audio or commits it to the old activity.
3. **Turn context + speaker-ID prepass.** `[TURN CONTEXT]` (time, reply-language
   reminder, current user) is sent as non-response text. The **current user is the
   VOICE speaker** identified this turn — it overrides the face-derived
   `current_user`, and falls back to the face identity when there is no voice ID
   (unknown / gate-reject / no transcript).

   **When each part runs depends on the mode**, because a voiceprint needs the
   completed utterance and therefore cannot exist at session open:

   | Mode | `[TURN CONTEXT]` sent | Speaker known? |
   |------|----------------------|----------------|
   | Always-listening (`wakeword=false`) | at session **open**, before any audio | No → face fallback, then corrected |
   | Wake-word / follow-up | after capture, once a final wake phrase confirms | Yes |
   | Deferred (noise-drop rebuild) | after capture, on the replacement session | Yes |

   In always-listening mode the speaker-ID prepass (`identify_and_decorate`, run
   **once** at session end) resolves the voice speaker *after* the context already
   went out with the face name. HAL then sends a `[TURN CONTEXT UPDATE]` correction
   naming the real speaker — still **before** `commit_audio()`, so it is part of the
   same turn. Skipped when the context already carried the right name, or when the
   turn is noise. The prepass result is reused downstream — speaker recognition
   never runs twice.

   **Gemini native-audio caveat:** `send_text()` drops **all** non-response text on
   Gemini `*native-audio*` models (`gemini_needs_idle_workaround()`), because
   repeated SDK `clientContent(turn_complete=False)` messages collide with later
   audio turns and close with WS 1011. On those models neither the context nor the
   correction reaches the reply, and the model falls back to whatever identity its
   session memory holds. `gemini-3.1-flash-live` and OpenAI accept both. Every drop
   is logged (`[realtime->model] DROPPED …`).

   **This does not apply to the shipped default.** `REALTIME_GEMINI_MODEL` defaults to
   `gemini-3.1-flash-live-preview` (`hal/config.py:734`), which is not native-audio, so
   the guard is off and both the context and the correction reach the model. It
   re-engages only when a `*native-audio*` model is configured.
4. **Commit.** At session end, if enabled + `available` + audio buffered,
   `commit_audio()` fires. A `thinking` emotion cue fires with the commit
   (face + servo + a FORCED LED pulse — `thinking` is normally a
   background emotion whose LED yields to the user's saved color; the
   realtime cue bypasses only that guard, user-LED-off still wins) and is
   cleared back to `idle` at the first output (first TTS sentence or first
   native audio frame) or when the turn dies with no output — unless the
   model already expressed its own emotion. This fills the 1-3s
   model-latency gap where the device otherwise looked frozen.

   The same commit arms the **dead-air filler** (`_WaitFiller`), the audible
   half of that cue. After `HAL_REALTIME_FILLER_DELAY_S` (default 1.5 s) with
   still no output, HAL calls `POST /api/sensing/filler` and os-server speaks
   one opening filler from its cache — os-server owns the phrase pools, the
   language, and the WAV cache, so the realtime wait and the main-agent wait
   sound alike. A normal chit-chat reply (~1 s) never reaches the timer; a turn
   the model grounds with Google Search, which emits no token until the search
   returns, does. The filler is interruptible, so the model's first sentence
   cuts it off; every exit path (reply, delegate, empty turn, exception)
   cancels the timer, and delegate cancels explicitly because the main-agent
   hop that follows fires its own filler. `0` disables.
5. **Consume.** `for output in stream_output()`:
   - `TextOutput` → sentences are flushed to TTS (`speak` / `speak_queue`).
     If `speak` returns busy (another non-interruptible TTS holds the
     speaker, e.g. an ambient nudge), the sentence falls back to
     `speak_queue` so the reply plays after it instead of being lost.
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
effect. A live edit triggers that restart immediately (`restartHAL` in
`system/device/service.go`).

**Restart only when the config changed.** os-server does *not* restart HAL on
every os-server restart — that would needlessly drop the voice pipeline. Instead
it hashes `config.json` and stores the hash in `config/.hal_config_hash` whenever
it (re)starts HAL. On boot (`handleSetUpCompleteChange` in `server/config_watch.go`)
it restarts HAL only when the current hash differs from that snapshot — i.e. the
config actually changed while os-server was down (fresh setup, OTA config swap, an
edit during downtime), or no snapshot exists yet (first boot). A plain os-server
restart with unchanged config leaves the already-running HAL untouched. If HAL is
genuinely down, `hal.service` (`Restart=always`, `RestartSec=5`) brings it back
independently, so skipping the restart is safe. The `restartHAL` path refreshes the
snapshot after it restarts HAL, so a live change followed by an os-server restart
doesn't double-restart. Hashing the whole file (rather than the HAL-read subset)
keeps the signal self-maintaining as HAL's read set evolves; the only cost is one
spurious HAL restart on the next boot after an os-server-only field changes.

### `config.json` `realtime` block

Modelled in Go at `system/server/config/realtime.go`; read in HAL at
`hal/config.py`. Shared fields sit at the top; per-provider knobs live in
`gemini` / `openai` / `qwen` sub-objects, with `provider` selecting the active
one (`none` or absent → realtime off). Empty `api_key` / `base_url` fall back to
`llm_api_key` / `llm_base_url` — **except qwen**: its credentials are its own
(`realtime.qwen.api_key` / `realtime.qwen.base_url`, Go struct `QwenRealtime`),
with deliberately **no fallback** to the shared `realtime.api_key`/`base_url` or
`llm_*` credentials, because qwen talks straight to the Alibaba MaaS host, not
through the `campaign-api` proxy. Set them via `realtime.qwen.*` in config.json
or via env on the device (`DASHSCOPE_API_KEY`, `HAL_QWEN_REALTIME_BASE_URL` in
`/opt/hal/.env`); with neither set the WS handshake fails loudly in the hal log.

> **Leave `base_url` blank unless you have a non-proxy endpoint.** (Applies to
> gemini/openai; qwen never derives from `llm_base_url` — it uses its own
> `realtime.qwen.base_url`.) When empty, HAL
> derives `<llm_base_url>/ws/gemini` (or `/ws/openai`) — the WS suffix the
> `campaign-api` proxy routes on. A `base_url` set to the bare `llm_base_url`
> (no `/ws/...`) is handed verbatim to the provider SDK and **404s at the Live
> handshake**. The web Settings "Base URL" field is therefore display-bound to the
> *explicit override only* (`RealtimeBaseURLOverride`, not the resolved value), so
> "leave blank to derive" stays blank and a save never re-persists the bare URL.

```json
{
  "wakeword": false,
  "realtime": {
    "enabled": true,
    "provider": "gemini",
    "gemini": { "model": "gemini-3.1-flash-live-preview", "voice": "Kore", "thinking_level": "MINIMAL" },
    "openai": { "model": "gpt-realtime-2", "voice": "alloy", "reasoning_effort": "minimal" },
    "qwen": { "model": "qwen3.5-omni-plus-realtime", "voice": "Ethan", "api_key": "sk-…", "base_url": "wss://…" }
  }
}
```

The reasoning knobs (`thinking_level` / `reasoning_effort`) default to the
**cheapest** tier (`MINIMAL` / `minimal`), not the providers' max — raise them
explicitly for deeper reasoning. Qwen has no reasoning/thinking knob at all, so
the web Settings page hides the Reasoning selector when qwen is the provider.
Knobs NOT in the block (turn detection, session
resumption, memory, summarizer) stay env/default-only.

**CoT-leak filter.** On `gemini-3.1-flash-live-preview` thinking cannot actually
be disabled: `thinking_level=MINIMAL` and `thinking_budget=0` are both accepted
but ignored (measured `thoughts_token_count` 125–168 on reasoning turns with
every config). Normally the thoughts stay internal, but on grounding/vision/tool
turns the server sometimes streams the model's whole text channel — English
planning ("The user is insisting…", "Phrasing draft:", "Delivery guidance:")
plus the real answer — into `output_audio_transcription`, while the model's own
audio carries only the clean answer. With native audio off HAL speaks the
transcription, so without a guard the leak is read aloud (burning TTS
characters) and forwarded as `[REPLY]`, where it re-enters context and
self-reinforces. `drivers/voice/_internal/cot_leak_filter.py` drops the leak at
sentence granularity before TTS and before the transcript is forwarded/saved,
in three tiers: TRIGGER markers (verb-bound third-person "the user is/wants…",
planning labels like "Phrasing draft:") always drop and switch the turn into
CoT mode; SECONDARY markers ("persona", "system prompt", "emotion tool", …)
drop only once CoT mode is on, so a legit reply about the device itself is
safe; in CoT mode, English planning sentences (non-English devices only —
non-Latin scripts like Vietnamese/Chinese/Japanese use an ASCII-ratio check,
Latin scripts like French/Indonesian additionally require English function
words so the real answer survives), quoted drafts, plan runts, and fuzzy
near-duplicates (CJK tokenized per character) drop too. The language check
ignores quoted spans, so an English planning sentence that embeds
reply-language text in quotes ("The search query 'cách dùng…' didn't yield…")
is still caught, while a reply-language sentence quoting English is not.
Every dropped sentence is logged as `CoT leak dropped`.

The main-agent path (openclaw/hermes replies spoken via os-server) has a Go
port of this filter — `system/server/agent/delivery/http/cot_leak_filter.go`
(adds a snake_case-identifier TRIGGER for the DeepSeek leak corpus); see
`docs/flow-monitor.md` § "CoT-leak filter (agent path)". Keep the two in sync
when hardening either side.

### Runtime configuration (`hal/config.py` + `config.json`)

Each `HAL_*` environment variable overrides its corresponding setting; `wakeword`
is a top-level `config.json` flag:

| Variable | Default | Notes |
|----------|---------|-------|
| `HAL_REALTIME_ENABLED` | `true` | Master gate for the realtime pipeline |
| `wakeword` | ROBOT.md `voice.wakeword` on a fresh config, else `false` | Top-level config-file wake-word gate. When true, a matching interim transcript is provisional only: HAL commits buffered audio to realtime or forwards a command only after an STT **final** result confirms a configured wake phrase. The transcript is split into sentences (`.` `!` `?`) and the phrase is accepted at the start **or the end** of any sentence; mid-sentence occurrences are rejected. The confirmation re-checks the assembled, still-punctuated transcript so the `\w+`-only merge step cannot retract a gate a partial opened. The supported prefixes are `hello`, `hey`, `hi`, `alo`, `okay`, `ok`, and `wake up`, applied to the permanent common alias (`hey autonomous`), device type (`hey lamp`), and current agent name (`hey Luna`). A runtime rename updates only the agent-name aliases. Bare names and other prefixes do not arm the gate. A rejected utterance is discarded and its transient listening LED restores to the normal resting state; it never leaves the persistent idle effect active. A confirmed turn opens the follow-up focus window; turns in that window are forwarded as `voice_followup` without another phrase. Every authorized turn dispatches to os-server: a spoken realtime reply becomes a silent `voice_agent_handled` sync event; unavailable, silent, failed, or delegated realtime follows the normal path. If realtime is disabled, the confirmed final transcript follows the normal os-server path. Missing/false preserves the pre-gate always-listening flow unchanged. On a config.json os-server creates, the initial value comes from the body's `voice.wakeword` (see Wake-word gate above); a config loaded without the key stays `false`. HAL restarts after a local Settings save or MQTT `wakeword.gate`. |
| `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S` | `20` | Idle seconds for the short post-command focus window. Each accepted `voice_command` or `voice_followup` refreshes it. `0` disables follow-ups and requires a wake phrase for every mic session. Ignored when `wakeword` is false. |
| `HAL_SILENCE_VAD_ENABLED` | `true` | Require Silero to confirm speech before the end-of-turn silence clock is refreshed. RMS remains the cheap pre-gate; set `false` to fall back to pure-RMS silence detection. |
| `HAL_SILENCE_VAD_WINDOW_FRAMES` | `3` | Number of frames batched per Silero run for that check — Silero costs ~20 ms/frame on ARM and its LSTM needs more than one 64 ms frame to settle. |
| `HAL_REALTIME_PROVIDER` | `gemini` | `none` \| `gemini` \| `openai` \| `qwen` |
| `HAL_REALTIME_TURN_DETECTION` | `off` | `server_vad` \| `semantic_vad` \| `off` (Gemini: off = manual activity detection) |
| `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` | `8.0` | Max seconds `receive()` waits for the next output event before ending a silent turn (fallback to main agent) |
| `HAL_REALTIME_LOOK_RECV_TIMEOUT_S` | `20.0` | Silent-turn watchdog used instead of the default for turns where a `look` fired (per-turn, via `extend_recv_timeout()`). Gemini's forced thinking over a text-dense frame can stay silent >8 s right before the answer — the default watchdog was killing those turns |
| `HAL_REALTIME_REQUIRE_TRANSCRIPT` | `true` | Never commit an empty-STT turn to the model. Real speech that nova-3 missed (short utterances) is voiced and passes the VAD/Silero guards, so committing its raw audio makes the model invent a reply to silence (a generic greeting, often with a name nobody said). When `true`, any empty-STT turn is dropped regardless of duration/voicing — silence beats a wrong reply. Set `false` to fall back to the Silero-gated audio-only path below. |
| `HAL_REALTIME_MIN_COMMIT_DURATION_S` | `0.8` | Sessions shorter than this with no STT transcript are treated as VAD noise and not committed to the model. Only consulted when `HAL_REALTIME_REQUIRE_TRANSCRIPT=false`. |
| `HAL_REALTIME_SESSION_IDLE_RESET_S` | `240` | Cost control: when a turn arrives after this many seconds of silence, recycle (rebuild) the session **after** that turn so the next turn drops the per-turn context the provider re-bills on a long-lived session. A post-pause turn is effectively a new conversation; long-term continuity survives via the reloaded `summary.md`. For native-audio Gemini, this is skipped when a successful pre-turn recycle already made the same idle gap fresh. `0` disables. Reuses the zombie-recovery rebuild path. |
| `HAL_GEMINI_SESSION_RESUMPTION` | `false` | Resume the same Gemini session across reconnects. OFF by default — the `campaign-api` proxy doesn't forward the resumption handshake, so resuming through it yields a zombie session (cold reconnects work). Enable only against an endpoint that supports it. |
| `HAL_GEMINI_PRE_TURN_RECYCLE_S` | `120` | Gemini transport guard: when a new spoken turn starts after this much idle time, rebuild the Gemini session **before** streaming pre-roll/audio so the turn does not hit a proxy/SDK idle-dead socket. `0` disables. A successful pre-turn recycle suppresses the generic post-turn idle recycle for that same turn, so one idle gap creates at most one cost/transport rebuild. |
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
| `DASHSCOPE_API_KEY` | — | Qwen key; overrides `realtime.qwen.api_key`. **No fallback** to `llm_api_key` / the shared `realtime.api_key` |
| `HAL_QWEN_REALTIME_BASE_URL` | — | DashScope workspace WS host; overrides `realtime.qwen.base_url`. Never derived from `llm_base_url` |
| `HAL_QWEN_REALTIME_MODEL` | `qwen3.5-omni-plus-realtime` | turbo is legacy: no function calls, ignores turn context |
| `HAL_QWEN_REALTIME_VOICE` | `Ethan` | 3.5-plus: also `Serena`; turbo-only: `Cherry` \| `Chelsie` |
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
| `voice_agent/qwen_realtime.py` | Qwen Omni Realtime provider (sync, raw WS, OpenAI beta schema; `_QWEN_RATES` cost table → `qwen_usage.log`) |
| `context_manager/{base,openclaw,hermes}.py` | Prompt + memory + skills assembly per gateway |
| `summarizer.py` | Anthropic-based memory summarizer |
| `config.py` | Provider config models (`GeminiConfig`, `OpenAIConfig`, `QwenConfig`) |
| `models/`, `enums/` | Input/output/event types, provider + gateway enums |
| `resources/` | System prompts (shared + per-provider) |
| `../voice/voice_service.py` | Integration: streams mic audio, consumes output, routes delegate/handled |
