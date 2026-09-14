# Realtime Voice Agent

Low-latency, speech-to-speech voice layer that runs **in parallel** with the
normal STT → agent pipeline. The realtime model handles casual conversation
directly (sub-second audio replies) and **delegates** anything that needs the
main agent (device control, skills, memory, real-time facts) back to the
OS-server flow.

Code lives in `hal/realtime/`; it is driven by
`hal/drivers/voice/voice_service.py`.

> **Source of truth:** this doc reflects the code. If they disagree, the code wins.

## Execution completion telemetry

For [voice KPI-3](voice-metrics.md#kpi-3-execution-completion-not-correctness),
`TurnDoneEvent.execution_completed` defaults to `false` and becomes true only
for a provider terminal signal: Gemini normal `generation_complete` or
`turn_complete` without interruption, or OpenAI/Qwen `response.done` with
`response.status == "completed"`. Connection close, send failure, synthetic
unblock, partial output followed by timeout, stale/replayed done, and an
abandoned receive are not completion evidence.

The base receive loop passes the observation to the orchestrator, which
snapshots it before recycling the session, then to
`RealtimeTurnResult.execution_completed`. HAL emits `realtime_turn_done` only
when the turn is handled and this flag is true. A memory-sync backend run
cannot establish completion for that realtime turn. This measures execution
ending, not answer correctness or full audio playback, and changes no routing
or playback behavior.

## Concept: handle vs. delegate

Every spoken turn is streamed to the realtime model *at the same time* as the
STT pipeline. At end-of-turn the model either:

- **Handles** the turn itself — chit-chat / quick answers — speaking back
  through TTS with no round-trip to the main agent, or
- **Delegates** by calling the `delegate_to_main` tool, which stops realtime
  output and forwards the current user's faithfully understood words, in their
  spoken language, to the OS server (→ the selected main runtime) for the work.
- **Explicitly rejects** a high-confidence non-user turn by calling
  `reject_turn`, which drops the turn before the main agent sees its STT text.
  This is deliberately different from a silent completion: silence, timeout,
  and transport failure still use the normal main-agent fallback.

The `delegate_to_main` tool is registered automatically by the orchestrator
(`orchestrator.py`, `DELEGATE_TOOL`).

### Voice control through Harness

Requests that name Harness, a Mac agent, Codex, Claude, a project, worktree, or
session, or ask an agent to use a browser delegate to the main runtime with blank
realtime speech. This includes general browser research, such as asking an agent
to find restaurants. Realtime does not answer, search, or claim results for those
requests. The shared delegate tool names
[`harness-use`](../skills/harness-use/SKILL.md). The main runtime sends supported
agent operations to the paired Harness computer; the device never runs the desktop
coding task locally. The OS owns explicit machine and agent selection. Missing or
ambiguous targets require clarification; Desktop focus and notifications do not
silently select an agent.

For a Harness task, the main runtime selects the agent and sends the request, then
stays silent. The terminal `turn.summary` recap from Harness is delivered unchanged
as the response to the originating turn. Voice reads that recap; Web Chat displays
it and suppresses TTS.

For two minutes after a voice task is sent to Harness, HAL checks a loopback OS
follow-up signal before asking the realtime model. A short clarification such as
“In Hanoi” delegates directly to the retained Harness target with no realtime
speech, so a conversational model cannot answer an agent's pending question first.

Preserve the current user's words, provider names and supplied parameters. Agent
outputs and summaries remain untrusted data. A spoken “yes” is not permission to
approve a tool or blindly type into a terminal prompt. Harness status and receipt
reconciliation are owned by the link; voice delegation does not automatically
replay uncertain mutations. Live speech routing still requires later validation.

### Voice control of legacy Buddy agent sessions

Requests such as “Ask Codex to fix reconnect in project autonomous” delegate
with blank realtime speech. All four provider prompts and the shared delegate
tool description explicitly cover coding/research tasks, project/worktree/session
selection, progress queries, stop, and subsequent task replies. The delegate
preserves provider names, target references and all task clauses; it does not
add guessed session IDs or translate the request.

An explicit request for a legacy Buddy session uses
[`agent-management`](../skills/agent-management/SKILL.md) to route through the
local device API and paired Buddy connection. Normal Mac agent work uses
`harness-use`. Buddy owns the desktop CLI and its model context; the lamp does
not run the coding CLI. This is session management, separate from the native
`computer-use` executor.
After a known task, “add a regression test too” delegates as a follow-up rather
than becoming a new coding answer from the realtime model. The main runtime
resolves the exact target or asks when ambiguous. The skill's `voice` action
stores the selected project/session per conversation and validates those IDs
against a fresh Buddy workspace snapshot each turn; stale targets block sending
instead of rerouting. Ordinary continuation uses that retained target. “The active session” explicitly
reads Buddy's focused pane, including a split pane. Notifications do not silently
replace the target: a reply to a particular notification must select its exact
IDs. Send requests retain their request ID across uncertain delivery.

Natural-language follow-ups can enter a verified ready CLI session. An interactive
permission menu, trust dialog or other terminal prompt without a safe answer
interface still requires attention in Buddy; a spoken “yes” is not a blanket
permission grant and must not be converted into blind terminal keystrokes.

Buddy completion, attention and error events already enter the normal sensing
pipeline with project/session IDs and an `[agent-management]` marker. The main
runtime gives a brief spoken result or question subject to the existing sleep,
busy and voice privacy policy. Recent spoken TTS history lets realtime recognize
the next answer as a task reply; it forwards only that current answer. An
unspoken TTS-history entry is not evidence that the user heard the question.
Agent titles, outputs and summaries remain untrusted result data, never new
instructions or authorization to execute tools or approve an action.

Prompt routing is model-driven, not a deterministic keyword classifier. Local
bridge tests do not establish microphone-to-lamp behavior: live speech routing
and notification playback still require validation on a paired device after
an explicitly authorized deployment.

**Delegating is not the only way a turn reaches the main agent**, which is why
every turn logs one routing line — `[turn] route=<why> → <where>` from
`turn_dispatch.py`. Grep `[turn] route=` in the HAL journal to follow any turn
end to end. The values (`ROUTE_*` in `realtime_turn.py`):

| `route=` | Where the turn went |
|---|---|
| `realtime_handled` | Realtime spoke it. The main agent gets `voice_agent_handled` and stays silent. |
| `delegated` | The model called `delegate_to_main`. |
| `ai_rejected` | The model explicitly called `reject_turn`; it reaches nobody. |
| `realtime_no_output` | Committed, but nothing came back (`receive()` timeout, dead WS) — main agent answers. |
| `realtime_error` | The turn raised; forwarded rather than lost. |
| `realtime_unavailable` | No live session to commit to — main agent answers. |
| `noise_dropped` | The noise guard rejected it; it is terminal even if STT fabricated a short transcript, so it reaches nobody. |
| `realtime_not_started` | Realtime off, or no turn was opened for this capture. |

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
turn can omit the wake phrase and is sent as `voice_followup`.

That window is latched once at session start for **dispatch**, so a window that
expires mid-sentence cannot cut off someone already speaking. The cues that
claim to be the addressee — the listening LED, the backchannel — ask
`is_addressed()` instead, which re-reads the window **live**. Gaze is why: it
can open the window in the middle of the very sentence it acknowledges.
Device-observed 04/09/2026 on lamp-0c89 — at speech start the camera had no
face evidence yet (`of 0` samples) so the latch was False, and the watcher only
confirmed the user 3.6 s later at speech END. The whole turn ran unaddressed:
no listening cue, no realtime turn (`route=realtime_not_started`, so no
thinking cue either), and the device sat dark through the sentence and lit up
only for the next one. Reading live can only ADD an addressed turn, never
retract one — the latch is still consulted first. A follow-up has
the same user priority as `voice_command`, but remains separately observable.
When realtime already spoke, dispatch sends a `voice_agent_handled`
synchronization event so the main agent records the exchange but stays silent;
unavailable, failed, timed-out, or delegated realtime takes the normal
main-agent path. This also consumes a one-turn vision handoff, so a temporary
Gemini failure cannot drop a voice command or leak a frame into the next turn.

For a newly granted gaze wake, VAD has already confirmed both speech and visual
intent before STT can produce its first partial. HAL therefore paints a dim,
LED-only blue breathing acknowledgement immediately. It does not freeze the
body or claim `listening`; the first partial upgrades it to the normal listening
cue. A no-partial session restores the preceding LED state when it closes (or
after a 3-second safety timeout), so ordinary VAD/noise sessions remain dark.

A realtime-handled turn also takes the speaker away from the main-agent turn
still in flight, not just its own. Its own run is silenced by `MarkSilentRun`;
the older one is silenced by a second cancel watermark (`autoSpeechWatermarkMs`,
see `docs/os-server.md`), stamped the moment the event arrives. Without it the
device answers the user's newest question in the realtime voice and then, a
moment later, answers the previous one in the main agent's voice. The main
agent runs one turn at a time, so this is a single stale answer rather than a
backlog.

The hook sits **before** the busy fork in `PostEvent`, not next to
`MarkSilentRun`. `voice_agent_handled` counts as passive, so a busy agent
queues it and returns early — and "the agent is busy" is exactly the case with
an older turn in flight, which made the later placement a no-op precisely when
it was needed.

The mark is deliberately weaker than the physical click: it never drops the
older turn's `[HW:]` markers, because an action the user genuinely asked for
must still run. Its pending fillers **are** dropped, though — the dividing line
is speech-versus-hardware, not click-versus-auto. A filler is a promise that an
answer is coming, not something the user requested, and leaving it armed
reproduces what the click had to fix: the device answers the new question, then
says "one moment" about the old one and goes quiet. The behaviour is opt-in per body: set `OS_REALTIME_SUPERSEDES_MAIN_REPLY=1` in the body's
`/opt/hal/.env` (os-server loads that file too). The code default is OFF,
because that default is what every body which has never heard of the switch
gets — lamp, intern-v2, reachy-mini, and any body with no `.env` at all.

Known gap, shared with the physical click: an event queued while the agent was
busy is given its runID at **replay** time, so it lands on the far side of the
mark and speaks even though the question predates it.

### A muted reply still reaches the realtime agent

The realtime session learns what the main agent replied through
`VoiceService.feed_realtime_history` — it persists the full text with
`save_main_agent_reply_fragment` (survives a session recycle) and pushes a
capped `[TTS HISTORY]` line into the live socket (does not).

That feed used to hang off the `on_speak_end` hook alone, so it only fired for
text that actually played. A turn muted by the physical cancel gesture is
dropped in os-server's `deliverTTS` and never reaches HAL, which left the
realtime session holding `save_main_handoff`'s "its spoken reply follows"
placeholder and no reply — the next turn then reasoned from a question it
believed had gone unanswered. os-server now posts that text to
`POST /voice/realtime/history` instead, which feeds the same two sinks without
the speaker.

The persisted fragment is the full reply either way: it is the processed
result, and memory wants all of it. Only the in-session line differs — it is
labelled `[TTS HISTORY, not spoken]`, because that line exists to stop the
model repeating what the user ALREADY HEARD, and on a cancelled turn they heard
none of it.

The second way a reply goes unheard is inside HAL, and os-server cannot see it:
`speak_queue` drops a superseded turn (an older `turn_seq` arriving after a
newer turn already owns the queue) and **returns success**, so the caller
believes it was spoken. This is the delegate case — the realtime agent hands a
question to the main agent, the main agent is slow, a newer turn wins the
speaker, and the answer evaporates while `save_main_handoff`'s placeholder
stays. The drop sites therefore call `_on_unspoken_reply`, a hook `VoiceService`
injects next to `_on_speak_end`, which routes into the same
`feed_realtime_history(..., spoken=False)`. It is gated on `realtime_feedback`
for the same reason the playback feed is: only the agentic runtime's own reply
may enter the model's context, never a dropped filler or system notice.

`turn_seq` is os-server's counter, but the threshold it is compared against
lives in the HAL process, and the two restart independently. A deploy, OTA or
crash restarts the count at 1 while HAL still holds the old high-water mark, so
every turn of the new session looks like a late arrival and is dropped —
measured 03/09/2026: `seq=1` against `latest_seq=40` silenced the wake greeting
(its LED and servo still ran) and would have silenced the next 39 turns. The run
id carries its creation time (`device-chat-<n>-<unix-ms>`), so when a LOWER OR
EQUAL sequence arrives from a run created LATER than the one holding the speaker,
HAL treats the counter as restarted and adopts the new sequence. Equal counts
because a restart resets to 1, so the new run only falls BELOW the old mark when
that mark is high — with a low mark (two restarts in a row, or a restart early in
a session) the sequences collide instead. Measured 04/09/2026: `seq=2` met an
unrelated `seq=2` from before the restart and the wake greeting was dropped, the
same silent-device symptom one comparison away. Ids without a stamp
(`tg-<messageID>`) keep the plain sequence rule: with nothing to compare, a
genuinely stale POST must not be able to take the speaker back.

### Two silence clocks (end of turn)

A mic session ends when the audio stays below the RMS threshold for the current
silence budget. There are two: once STT has delivered a **final** segment for
this turn, the provider has already decided the user stopped (Flux emits
EndOfTurn, nova fires `is_final` after its own endpointing window), so the loop
closes `ENDPOINT_SILENCE_S` (`HAL_ENDPOINT_SILENCE_S`, default 0.8s) **after
that final arrived** — not after the last speech. The distinction is the whole
point: Flux emits an EndOfTurn for a breath pause *inside* one utterance, and
measuring from the last speech applies the short budget retroactively to
silence already spent, so such a final closes the session on the next frame
while the user is still talking (device-observed 04/09/2026 on lamp-0c89:
final `'Hello.'` at 09:22:50.766, session closed 114ms later, mid-sentence).
Running the clock from the final gives the speaker a real window to carry on.
The short clock applies only when `final_ts >= last_confirmed_speech`: if
confirmed speech continues after that final, `turn_should_close` returns to the
2.5s fallback until a new final arrives. An old final cannot shorten a later pause.
Sitting on the long clock after that evidence is dead air in front of every
realtime commit — it was the largest fixed cost between the user falling silent
and the model hearing the audio. With no current final in hand there is no such
evidence, so resumed speech and empty or noise-only sessions use the long fallback clock,
`SILENCE_TIMEOUT_S` (2.5s). Set `HAL_ENDPOINT_SILENCE_S=0` to go back to the
single long clock; raise it if the device starts cutting people off at natural
mid-sentence pauses.

The rest of this section is about the clock itself, and applies to both. RMS alone is not enough in a noisy room: room noise sits
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

### The mic ignores our own backchannel cue

Backchannel listening cues ("Ok", "Mm", "Oh") are played on purpose **without**
setting the TTS `speaking` flag, because that flag ends the running STT session —
the one the cue exists to keep alive. But `speaking` is also the only thing that
normally keeps the mic off while the device talks, so the cue reached the mic
unfiltered and the entry VAD opened a **new** session on it about a second later.
Device-observed 19/08/2026: `'Ok'` came back as `transcript='Okay.'` and `'Oh'` as
`transcript='no'`, each running as a real turn that no user spoke.

`Backchannel.self_audio_active` closes this without touching `speaking`. `_play()`
arms a deadline (clip length + `HAL_BACKCHANNEL_ECHO_TAIL_S`) *before* the first
sample leaves, then re-anchors the tail to when playback actually ended. While it
holds, the VAD loop drops those frames from the speech test **and** from the
pre-roll lookback — keeping them in lookback would just replay the cue as the next
session's opening audio — and resets Silero's LSTM on resume, the same cleanup the
warm-mic drain does. Only session *opening* is suppressed; a session already
streaming is untouched, which is the whole point of the feature.

Each cue is also bound to the STT-session epoch that scheduled it. If normal TTS
holds the output stream long enough for that source session to end, the queued cue
is cancelled immediately before playback; it cannot leak into a newer mic session
as a fabricated transcript. This cancels only optional device speech — it never
closes, clears, or mutes user microphone capture, so the user can still talk
over it.

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

## Echo cancellation (AEC)

`hal/drivers/voice/aec.py` runs the mic through WebRTC's APM (AEC3) with the
audio being played as the reference. It is **provider-independent**: the
reference is tapped in `_WatchedStream.write` (`tts/service.py`), the single
point every playback path reaches the device through — synthesized speech, the
`speak_queue` drain, and realtime **native audio**. Tapping there rather than at
synthesis is deliberate: TTS renders a sentence far faster than real time, while
the output stream writes at playback rate, which is the timing the mic sees.

**On by default** (`HAL_AEC_ENABLED=true`). Absent the binding below every AEC
entry point degrades to a no-op, so defaulting it on cannot break a device that
lacks it. It needs the `aec-audio-processing` binding, which is **not** a base
hal dependency — PyPI ships no Linux wheels for it, so a device builds it from
source. It lives behind the `aec` extra (`uv sync --extra aec`), deliberately
kept out of `dependencies` and out of `hardware`: the build needs meson/ninja,
which the lamp image does not install, so a hard dep would break both the image
build and `software-update hal` for a feature that degrades to a no-op without
it. When the import fails, `configure()` logs once and every entry point becomes
a no-op; the voice path behaves exactly as before, so the default is safe on a
device without the binding.

The canceller cleans the mic; it does not decide interruption. Nothing on the
turn path listens to the cancelled mic for the user talking over a reply — a
local detector was tried and removed after the measurement recorded below. The
user interrupts by tapping (GPIO button, TTP223 touchpad), and inside a live
session the provider's VAD owns interruption (see *Live mode*).

| Env | Default | Meaning |
|-----|---------|---------|
| `HAL_AEC_ENABLED` | `true` | Master switch |
| `HAL_AEC_DELAY_MS` | `205` | Speaker→mic delay hint. **Per-device** — measure it, don't inherit it |
| `HAL_AEC_NS` | `true` | Also run APM noise suppression. Carries most of the cancellation on this hardware |
| `HAL_AEC_TAIL_S` | `2.0` | Keep cancelling this long after the last speaker write, then bypass the APM |
| `HAL_AEC_REF_MS` | `500` | Echo-reference FIFO depth |
| `HAL_AEC_DUMP_DIR` | — | Write `aec_mic/ref/out.wav` for offline ERLE analysis |

### Installing the binding

PyPI publishes **Windows wheels only** for `aec-audio-processing`, so every
other platform builds from its sdist. That sdist vendors the full
webrtc-audio-processing + abseil sources and a pre-generated SWIG wrapper, so
the build is self-contained: it needs no system `libwebrtc-audio-processing`
and no system SWIG. Its build requirements (`swig`, `meson`, `ninja`, `cmake`)
all ship wheels on PyPI, so **no `apt install` is required** — which matters,
because an end user cannot run apt on a shipped device.

The build itself is the problem: measured on a lamp (A523, 8 cores) it takes
**5m35s wall / 36m CPU**. That is fine once, on a developer's device; it is not
fine on every device, every image build, and every `software-update hal`. So the
project builds one wheel and attaches it to a GitHub release:

```bash
scripts/release/build-aec-wheel.sh <device-ip>   # → dist/aec/*.whl
make upload-aec-wheel                            # → CDN, prints URL + sha256
```

`build-aec-wheel.sh` compiles on the device, in `/tmp`, in a throwaway venv with
meson/ninja from PyPI — `/opt/hal` and the system packages are never touched —
then copies the wheel back, installs it into a clean venv to prove it imports,
and deletes its scratch directory.

**Build on the OLDEST target, not the newest.** The wheel links only
`libstdc++/libm/libgcc_s/libc` and requires **glibc ≥ 2.34**. glibc is forward
compatible, so a wheel built on the lamp (Debian 12, glibc 2.36) also runs on
Reachy Mini (Debian 13, glibc 2.41) — the reverse does not hold. The wheel is
tagged `cp312-cp312-linux_aarch64`: `uv` on every body runs CPython 3.12, so
that tag covers the fleet, and `upload-aec-wheel.sh` refuses to publish anything
else rather than let the mismatch surface on a customer device.

The asset lives on a per-wheel tag (`wheels/aec-<version>`), never the OS
version tag — the wheel does not move with OS releases, and a dedicated tag is
not re-pointed, so a pinned URL cannot change content under the lockfile. It is
a GitHub release rather than the OTA bucket on purpose: this repo is public, so
a fork can build and host its own wheel, while the bucket is org-only.

`hal/pyproject.toml` pins that URL under `[tool.uv.sources]`, scoped to
linux/aarch64/CPython 3.12. Measured on `lamp-0c89`, installing the hosted wheel
takes **1.9 s** against **5m35s** to compile. Anything outside that marker —
a dev Mac, a future 3.13 — falls back to the PyPI sdist and compiles, so
`uv sync --extra aec` always works; only the fast path is pinned.

The main VAD loop is wrapped, and with `HAL_WARM_MIC=true` (now the default)
the mic stays open through playback, so cancellation runs during the device's
own speech. The reverb gate is deliberately left uncancelled so its timing is
unchanged.

**Measured on a lamp** (OrangePi sun60 / A523, USB mic + USB speaker — two
independent clock domains). The delay hint is per-device because the two USB
clocks free-run: on `lamp-ee17` the real lag is 204 ms median over one 93 s take
and 192 ms over another, drifting 154→215 ms within a single take (~667 ppm).
Correcting 150→205 raised achieved ERLE from 15.2 to 17.9 dB. An earlier
80→150 correction on the same unit took it from 10.9 to 18.6 dB.

Cost is ~3.9 % of one A523 core at realtime. The MacBook reference figure for
the same canceller is ~42 dB; the gap is the hardware — two free-running USB
clocks and a cheap analog path. Only ~1.6 dB of the echo here is *linearly*
predictable (coherence 0.31), so nearly all cancellation is suppression, which
is why turning `HAL_AEC_NS` off costs ~10 dB of ERLE and triples the residual.

### Known limitation: the reference starves

`EchoReference` is a FIFO tapped when ALSA **accepts** audio, but the mic hears
that audio a full output buffer later, and TTS writes in network-paced bursts.
When writes run further ahead than the FIFO is deep, the oldest bytes — exactly
the ones the mic is about to hear — are dropped, and the reference then runs dry
for the rest of the burst. Measured on `lamp-ee17`, the reference underran on
**30–86 % of processed frames** during a reply, and ERLE per window swings from
−25.1 dB to 23.2 dB accordingly. On the frames where a reference *is* present
the canceller reaches 15–23 dB, so the deficit is starvation, not the APM.

Deepening the FIFO does not fix it and makes it worse (`HAL_AEC_REF_MS=1500`
measured 3.6 / 2.3 dB against 23.2 / 19.1 dB at 500) because the lead becomes
variable and exceeds AEC3's alignment window. The real fix is to pace the
reference to playback time rather than write time, plus a dedicated capture
thread so the mic stops draining `arecord` in bursts.

The pacing half is implemented: `_WatchedStream.write` slices each caller
buffer into `TTS_REF_SLICE_S` (40 ms) pieces and publishes the reference only
after the device has accepted that slice, so the loop advances at roughly
speaker rate. The slice size is a GIL tradeoff, not an acoustic one — every
slice costs one blocking PortAudio write plus one reference write in Python,
and at 10 ms the ~1600 round trips per reply were audible as playback stutter
on a board whose main thread is already saturated by vision. The dedicated
capture thread is still not implemented.

Cached WAV cues (including the single-click acknowledgement) also submit 40 ms
blocks to this wrapper. Their previous outer 10 ms loop defeated the wrapper's
batching and kept 100 device/AEC calls per second under vision load. Cached
playback now uses 25 calls per second, plus a final partial block, and checks
cancellation between blocks. This can add up to 30 ms to the interval between
stop checks compared with the old loop. This is not an acoustic stop bound:
audio already queued in the output device can remain audible after a stop.

Regular provider TTS (including ElevenLabs PCM at 24 kHz played at 44.1 kHz)
uses continuous linear resampling across network PCM chunks within each
synthesis request. It retains the boundary sample and sample clock instead of
restarting interpolation at every chunk. Normal EOF flushes the held final
sample to preserve `ceil(N * output_rate / input_rate)` output samples for `N`
input samples; cancellation does not flush a tail. Head, tail, and queued
synthesis requests each have independent resampling state. Native realtime
resampling and whole-file cached WAV resampling are unchanged.

AEC reference resampling caches SciPy's default Kaiser FIR coefficients by
reduced sample-rate ratio and dtype (up to 32 entries), avoiding filter design
on every speaker write. `resample_poly` still performs its usual gain and
padding; the reference waveform and FIFO pacing remain unchanged. Before the
first speaker write of a playback, HAL prepares the reference filter so a cold
SciPy import/filter design cannot stall playback after its first 40 ms. This
preparation is skipped when AEC is inactive or sample rates match. Cancellation
is checked between slices, including after preparation; a stop acknowledgement
chime can still play while the speech stop flag is set.

Playback stream creation requests `max(0.120s, default_high_output_latency)`
and logs the actual negotiated latency. The previously observed device default
of 43.5 ms barely covered one 40 ms write slice. The 120 ms request provides
three slices of scheduling headroom while preserving larger defaults such as
Bluetooth outputs; it is neither a guaranteed buffer size nor a fix for network
jitter. More queued audio can extend the audible tail after cancellation.

`_WatchedStream.write` combines PortAudio's underflow results across all slices.
Mid-playback underflows are logged at most once every 5 seconds with a cumulative
count, `writer_gap_ms`, and `previous_aec_ms`; playback boundaries (which may
follow idle) are debug-only and
keepalive writes are excluded. These output underflows report playback starvation,
whereas an AEC reference underrun only reports missing cancellation-reference
samples and does not establish a speaker underrun. These local playback changes
still require listening verification on hardware.

`aec.uncancelled()` reports whether the frame just read went through *without*
real cancellation — reference underrun, bypassed stream, or mic overrun. The
live uplink gate keys on it in `cancelled` mode so it never sends raw echo up
as the user. Note what it does **not** say:
it reports whether a reference *arrived*, not whether cancellation *worked*, so
a frame with 0.9 dB of ERLE still counts as cancelled.

### Why there is no voice-driven interrupt on the cancelled mic

A local detector ("barge-in": stop TTS when the user talks over it) shipped
between 25/08 and 13/09/2026 and was removed. The measurements are kept here so
it is not retried from scratch. The residual that survives cancellation is loud
enough to look like a user interrupting, and it **is** speech, so neither a
level gate nor a speech classifier can reject it. Measured in a silent room,
echo ceiling against real interruptions:

| Speaker volume | Mixer | Echo ceiling | Real interruption |
|---|---|---|---|
| 25 % (`lamp-ee17`) | −45 dB | 9804 | 8027 |
| 40 % (`lamp-0c89`) | −36 dB | 9969 | 6956–8027 |
| 65 % (`lamp-0c89`) | −21 dB | 13560 | 6956 |

The echo ceiling sits **above** the real interruptions at every volume, so a
threshold below it self-interrupts and one above it misses ordinary speech.
Lowering the speaker is not a workaround either: 24 dB of mixer range moved the
ceiling by under 3 dB, because the coupling is not dominated by the airborne
path.

The last defence tried was an envelope test on the **raw** mic: align the
candidate window against the retained reference, subtract it plus the learned
coupling gain, and read the *skew* of the residual rather than its size — a
person can only add energy, so a one-sided residual is someone else in the
room. Labelled on `lamp-0c89` at 40 %:

| | Residual skew |
|---|---|
| Echo, silent room (15 windows) | −2.8 … +2.1 dB |
| Echo, mixed run (~40 windows) | −50.0 … **+4.8** dB |
| Confirmed real interruption | **+8.4** … +40.4 dB |

That looked separable, and 12 replies into a silent room fired zero false
interruptions. It did not hold: 27/08/2026, 20 replies into a silent room, the
lamp cut itself off 4 times, and scoring 79 labelled windows (69 echo, 10
confirmed interruptions) against every feature available — envelope
correlation, residual skew, coupling offset, APM suppression, and the textbook
magnitude-squared coherence — gave a best AUC of 0.72, with every threshold
that reached 0 % self-interruption missing 90–100 % of real interruptions:

| feature | echo | person |
|---|---|---|
| coherence | 0.03 … 0.60 | 0.04 … 0.57 |
| correlation | 0.25 … 0.99 | 0.41 … 0.95 |
| skew (dB) | −6.5 … 67.1 | −1.0 … 119.7 |

The classes overlap almost completely, so no classifier on this signal will do
better. The cause is upstream: AEC3 reaches only ~6 dB ERLE on this hardware
because the speaker and mic are separate USB devices with free-running clocks,
and during double talk the APM does not attenuate the near-end talker — it
removes them (a frame reading 7426 on the raw mic left the APM at 5). Comparing
the cancelled signal instead of the raw one was also tried and is worse: the APM
is a time-varying gain and eats the loudness contour.

Re-attempt only after the echo path itself improves — a neural canceller
(DTLN-aec runs real-time on a Pi 3 B+) or one sound card for both directions —
and gate the attempt on the acceptance test in *Live mode*: replay
`barge-in-captures/full40-bargein-off` through the canceller and require the
peak residual under the real-interruption floor (6956) with margin. Until then,
tap-to-interrupt and the provider's VAD are the two interruption paths.

`process()` buffers to the APM's fixed 10 ms frames and returns exactly as many
samples as the caller asked for (priming once with up to 10 ms of silence), so
hal's 64 ms framing is unaffected. ERLE is logged periodically while the
speaker is active — **0 dB means the canceller is doing nothing**.

> The image already loads PulseAudio's `module-echo-cancel` (`setup.sh`), but
> nothing reaches it: a udev rule sets `PULSE_IGNORE=1` on the speaker codec so
> hal can own it, and capture goes through `arecord -D plughw:` directly. That
> module has no reference and no client; it is not what cancels echo here.

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
2. answers the call with `FunctionCallResultInput`, whose `trigger_response`
   depends on whether the model has already spoken this turn
   (`orchestrator.py`, `_handle_emotion_call`). If it **has** spoken
   (`trigger_response=False`), the result is recorded without spawning a second
   model response — for OpenAI and Qwen this skips `response.create`
   (`openai_realtime.py` / `qwen_realtime.py`); on Gemini the ack is not sent at
   all, because `send_tool_response` there *continues* the turn and makes the
   model re-speak its whole reply. If it has **not** spoken yet, the tool call is
   the entire generation so far and Gemini pauses until answered, so the ack is
   sent (`trigger_response=True`) or the turn deadlocks until the watchdog fires.
   Net added latency to speech ≈ 0.

### Pending-tool-call session quarantine (Gemini)

Gemini Live **refuses `send_realtime_input` while a tool call it emitted is
unanswered**, and enforces this by closing the session with WebSocket **`1008`**
("The operation was aborted"). This is a deliberate policy close by the provider,
not a dropped stream — a transport drop shows up as `1006` with an empty reason
and is handled by the proxy, not here.

`gemini_live.py` therefore quarantines the **whole client side** of that
session, rather than merely gating microphone audio:

- receiving a `tool_call` registers every `call_id` in `_pending_tool_calls` and
  makes the session non-sendable;
- while any call remains unresolved, **all client input is suppressed**:
  `AudioInput`, manual-VAD `activityStart`, `activityEnd`, commits, and other
  client messages. Nothing is buffered for replay, because it would turn speech
  captured during an invalid provider state into a stale later turn;
- for a normal `FunctionCallResultInput`, the call stays pending until Gemini
  has accepted `send_tool_response`. Only that successful provider acknowledgement
  clears the call and makes the same session usable again. A failed or rejected
  acknowledgement leaves the session quarantined and it is discarded;
- the fire-and-forget `express_emotion` path above deliberately sends no Gemini
  acknowledgement after speech has started, because doing so makes Gemini repeat
  the reply. Such a session can never become valid again: it remains
  non-reusable and the next `prepare_turn()` rebuilds a fresh session;
- there is no expiry or other timeout that reopens a quarantined session. A
  fresh/rebuilt session has no inherited pending calls.

In particular, `_async_commit` suppresses `activityEnd` while quarantined too.
Completing an old activity bracket is not safe when Gemini is waiting for the
tool result; the replacement session starts its next activity cleanly.

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

When the user asks about what the device **sees** ("what is this?", "look at this",
"look at what I'm holding", "what am I holding?", "read this label", "what colour is
this?"), the realtime model answers in-session instead of delegating. Note "look at
this" routes here, **not** to the camera privacy toggle — `skills/camera/SKILL.md`
disambiguates the verb by what follows it, since "look at me" means "turn the camera
on" while "look at this" is a question about an object. This only applies to turns
that are **purely** a question about what it sees: if the same turn also contains an
action ("turn to the right, hold it there, and tell me what you see"), the prompt
requires a single `delegate_to_main` covering both halves — no `look` — so the
movement is never silently dropped. The orchestrator registers a `look` tool
(`orchestrator.py`, `LOOK_TOOL`) and handles the call in `_handle_look_call`:

1. **Aim the head at the subject first**, on devices that can move — otherwise a
   confident answer gets given about whatever the head happened to face. See
   [Look-aim](../robots/lamp/docs/vision-tracking.md#look-aim--pointing-the-head-before-a-visual-question-captures)
   for the aim loop, how it picks which person is the one asking, and the
   remembered bearing it falls back on when nobody is visible.
2. Grab a **sharp** camera frame **in-process** (`_capture_frame` calls
   `capture_still` — no HTTP loopback; servos are frozen (animation loop +
   tracker worker both honor the flag) and the frame is only accepted once its
   capture timestamp is past the settle after the last servo bus write, so motion
   blur can't reach the model. The settle is 0.3s, scaled up with the size of the
   last aim correction to a 0.5s ceiling — an aim that exits on its deadline does
   so straight after a large swing, and the arm is still ringing past a flat
   300ms; zero added latency when the servos are already still or the device has
   none), downscaled to `HAL_GEMINI_VISION_MAX_WIDTH`
   (default 768px) to bound image tokens.
3. Enqueue it as realtime **video input** (`ImageInput` → `send_realtime_input(video=…)`),
   then **replay the turn**: the Live API queues a frame sent mid-turn for the
   NEXT turn (device-proven: the tool-ack → continue-turn flow answered every
   look from the *previous* look's image — a one-image lag no ack delay fixes),
   so instead of acking the tool call, the orchestrator yields `LookReplaySignal`
   and `run_realtime_turn` re-appends the turn's audio and commits again on the
   SAME session. The queued frame joins the replayed turn.
4. The replayed turn re-triggers `look`, which hits the reuse guard
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
stale image) and, when fresh (`HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S`, default 45s),
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

**Idle parking.** A Gemini session nobody is talking to is closed by the server
with WS `1008` "The operation was aborted" (measured idle lifetimes: 86-198 s).
That close costs no turn — the pre-turn recycle above already replaces the
session before any post-idle turn streams — but the backend logs it as an error
and alerts on it, so the device closes first. After
`HAL_GEMINI_IDLE_PARK_S` seconds with no turn activity, an `rt-idle-park`
watchdog thread disconnects the transport and marks the session *parked*. A
parked orchestrator still reports `available`: the next `prepare_turn()`
reconnects a fresh session synchronously (`idle-park-resume`) before any audio
is streamed, which is exactly what the pre-turn recycle would have done for that
turn anyway, and `voice_service` buffers the capture across the ~1 s handshake.
Parking is skipped while a turn is in flight, and a resume that cannot connect
reports unavailable (turn falls back to the main agent) while staying parked so
the next turn retries.

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
  That gap window alone cannot tell a model that chose not to answer from one
  that is **working**: a turn grounded with Google Search emits no output at all
  until the search returns, and ending it there throws away an answer the model
  was about to give, hands the turn to the far slower main agent, and still pays
  for the abandoned search (whose chunks land in the session context and are
  re-billed on every later turn). The two are not alike on the wire, though — a
  working turn keeps sending messages that never reach the queue (thought parts,
  grounding metadata, usage-only frames), while an abandoned one goes completely
  quiet. Providers call `note_server_activity()` on every inbound message, and
  `receive()` keeps the turn alive past the gap window while those keep arriving,
  up to `HAL_REALTIME_TURN_MAX_SILENCE_S` (default 20 s) for the whole turn.
  A turn with no inbound traffic at all still ends on the first gap window.
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

The call is **retried** (`HAL_REALTIME_SUMMARIZER_RETRIES`, default 2, backoff
`HAL_REALTIME_SUMMARIZER_RETRY_BACKOFF_S`) because the failures come from the
gateway, not from the input: measured on lamp-0c89 03/09/2026, the same payload
returned 404 once and then succeeded on four of the next five attempts (one
timed out), while the LARGER payload containing it went through first time. A
dropped call otherwise costs the whole summary until the next session rebuild.
This includes a turn delegated or fallen back to the main agent: HAL persists the
user request before dispatch, then persists every opted-in main-agent TTS reply
fragment when it finishes speaking. `[TTS HISTORY]` still updates the current
live session immediately, but it is not treated as durable memory: an idle or
tool-call session replacement starts from the JSONL/summary instead.
For OpenClaw-layout runtimes (OpenClaw, PicoClaw, Codex, Claude Code, OpenCode),
the context manager also loads the workspace-root `MEMORY.md` in addition to the
derived device summary and recent `memory/*.md` files. Hermes instead uses its
native `memories/MEMORY.md`.
Summarization runs at `start()` (catch-up) and `stop()` (flush). The `start()`
catch-up runs in a **background thread** (after `connect()`), so the Anthropic
call never blocks the session from becoming `available` — otherwise an early
turn ("hello") right after a restart would leak to the main agent.

## Live mode (full duplex)

**What it changes.** The local VAD stops being an endpointer and becomes a
**doorbell**: it decides when to OPEN a session, and once one is open it does
not run at all. The mic streams continuously and the **provider** owns turn
taking, end-of-turn and interruption. Enable with `HAL_LIVE_MODE=true`.

Measured on `intern-v2-6286` (2026-09-07, Gemini 3.1 Flash Live): **97 ms** from
last audio sent to first audio received, against 3-6 s commit→first spoken
sentence on the turn path.

**Exclusive by design.** The turn path and a live session need *opposite* turn
detection, and that setting is baked into the provider session at connect time.
Supporting both at once would mean a runtime override plus a session rebuild on
every entry and exit; making live mode a whole-process choice removes that
machinery entirely, at the cost of a restart to switch. So `HAL_LIVE_MODE=true`
**forces** `HAL_REALTIME_TURN_DETECTION` from `off` to `server_vad`
(`hal/config.py`) — that value is read at import by `GeminiConfig.vad_enabled`,
so it must be settled before those models are defined. An explicit non-`off`
value is left alone. Getting this wrong produces a device that streams audio
forever and never answers.

### Entry: two gates, three outcomes

`_vad_loop` confirms speech as usual, then `_live_decision()` returns one of:

| outcome | when | cost |
|---|---|---|
| `live` | trigger is speech and realtime is available | one live session |
| `turn` | realtime disabled or unavailable | the normal turn path, so the device still answers via STT + main agent |
| `skip` | music playing, or the trigger was **not speech** | nothing at all |

The second gate is not optional on a device with a wide entry VAD. A live
session bills upstream audio for as long as it stays open, so it must not fire
on a click — and the entry gate cannot be trusted with that alone: `webrtcvad`
accepted 7/7 non-speech probes where Silero rejected all 7, and
`HAL_SILERO_ENABLED` is `false` on some devices. `_rt_noise_is_speech()`
therefore carries its **own** Silero instance. Device-observed on
`intern-v2-6286`: a clipped codec transient opened a session every ~25 s all
evening. `skip` (never `turn`) is also why a rejected trigger costs no STT
session either.

### The uplink gate

Every mic frame reaches the model. The only per-frame decision is whether the
canceller can vouch for it, and a frame it cannot is **replaced by silence of
the same length, never dropped** — the uplink is a clock, and a splice is
exactly what a server-side VAD reads as an onset. Substitution happens *before*
resampling, so frames-out == frames-in by construction.

`HAL_LIVE_UPLINK_DURING_PLAYBACK`:

- **`mute` (default)** — substitute silence for the whole playback window.
  Ships today. Costs barge-in entirely: the user cannot interrupt until the
  device stops talking.
- **`cancelled`** — send the cancelled frame; substitute only frames
  `aec.uncancelled()` flags. True full duplex.

`cancelled` is the intended end state and is **not** the default, because the
canceller does not yet earn it. Measured 2026-09-04 (`barge-in-captures/`): mean
ERLE 14-19 dB but **peak ERLE ~5 dB** — mic peaks of 29264 leave the APM at
25269, against a real-interruption floor of 6956. The provider's VAD sees peaks
and has no echo defence of its own (it cannot see `uncancelled()` and applies
no duration floor), so it reads the device's own onsets as the user
interrupting. Flip to `cancelled` once a replay of `full40-bargein-off` puts
peak residual under that floor with margin.

Two independent "is the speaker live" signals feed the gate, because neither
alone suffices: `tts.speaking` is authoritative and works with **no canceller at
all** (with none, `aec.reference_idle_for()` returns `inf` and the gate would
never fire), while the reference tail adds the acoustic decay after the flag
drops. `HAL_LIVE_PLAYBACK_TAIL_S` is the *acoustic* tail, deliberately not
`AEC_TAIL_S` (2.0 s): keyed on the longer one, `mute` swallows the first two
seconds of every reply the user gives.

**Music is excluded by asking `music_service`, never by asking the gate.**
`aplay`/`paplay` write straight to ALSA and never reach the echo tap, so during
music `reference_idle_for()` reads as a perfectly quiet room.

### The output pump

`_live_out_pump` loops `orchestrator.stream_output()` rather than reading the
agent queue directly. That reuses the entire existing tool surface — `look` +
replay, `express_emotion`, `reject_turn`, `delegate_to_main` — instead of
reimplementing it, and it re-reads `self._agent` on every outer iteration, so a
session rebuild cannot leave the pump reading a dead queue. `stream_output()`
returns once per model reply (`turn_complete`) and also on a quiet stretch when
`receive()` times out having yielded nothing; both simply mean "go round again".

`InterruptedOutput` (new) is emitted by `gemini_live` on `content.interrupted`
and is **the only voice-driven interruption there is** — nothing local decides
it. It stops playback at once. The turn-based path never sees one: manual VAD
gives the server no opening to emit it.

**Outputs are flushed once at session start.** Without it the session opens on a
stale output from the preceding turn — device-observed 2026-09-07: a
`reject_turn` queued by the previous noise turn was read in the same second as
session START, and the session then sat silent for its whole idle timeout.
`run_realtime_turn` flushes before every commit for this reason; a live session
commits nothing, so it flushes on entry instead. Once only — flushing per reply
would discard output the model is still streaming.

### Session recycling is suppressed

`set_live_active(True)` suppresses every post-turn recycle in `stream_output()`
for the duration of the session, because all three reasons are wrong inside one:

- **zombie** — a quiet stretch produces no output, so
  `REALTIME_ZOMBIE_RECONNECT_AFTER` of them (~24 s of the user simply not
  talking) would force a reconnect mid-conversation.
- **turn-cap** — swaps the session every `REALTIME_SESSION_MAX_TURNS` replies.
- **idle** — same swap, same open uplink.

A rebuild is also not survivable here the way it is between turns: the mic pump
keeps appending straight through the swap. Deferred, not cancelled —
`set_live_active(False)` resets the counters at hangup.

### What live mode does not do

Everything below hangs off the turn boundary and has no supply in a live
session. These are product trade-offs, not bugs:

| lost | consequence |
|---|---|
| STT transcript | no `[TURN CONTEXT]`, no transcript-based filters |
| wake word | confirmed from STT text, so `HAL_WAKEWORD_ENABLED` has no effect in live mode — entry is VAD-only |
| speaker ID, speech emotion | a session produces neither |
| local STT on delegation | `delegate_to_main` ends the session and forwards `[voice-instruction]` + Gemini's own input transcription as `[transcript]` (`FunctionCallOutput.user_transcript` → `DelegateSignal.transcript`, read by `_live_out_pump`); the main agent's reply plays after hangup. Providers without input transcription forward the instruction alone |

Also not run inside a session: the RMS entry gate, `SPEECH_HOLDOFF_S`, the
silence clock, `MAX_SESSION_DURATION_S`, the per-turn STT socket and its
keepalive (not even pre-connected — `stt_keepalive_on` is false in live mode),
the noise guard, the warm-mic drain and echo-skip, and `commit_audio`. None are deleted: the turn path still uses every one of them,
and gets them back the moment a session ends.

### Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `HAL_LIVE_MODE` | `false` | Whole-process live mode. Forces `HAL_REALTIME_TURN_DETECTION=server_vad` when that is `off` |
| `HAL_LIVE_UPLINK_DURING_PLAYBACK` | `mute` | `mute` (no barge-in, ships today) or `cancelled` (true full duplex, needs the AEC fix) |
| `HAL_LIVE_PLAYBACK_TAIL_S` | `0.35` | Acoustic tail after the last reference write during which the room still counts as playing |
| `HAL_LIVE_IDLE_HANGUP_S` | `15` | Hang up after this long with no action **from the user**, measured from whichever came later: the user's last words or the moment the device stopped speaking |
| `HAL_LIVE_MAX_UNPROMPTED_REPLIES` | `3` | Hard ceiling on consecutive model replies with no user speech between them — breaks a self-talk loop without cutting one long answer short |
| `HAL_LIVE_MAX_S` | `600` | Absolute ceiling on one session |

### Known limitation: `mute` can self-trigger

With `mute`, the uplink carries **digital silence** for the whole playback
window and real room audio after it. That transition is an amplitude onset, and
a server-side VAD reads an onset as somebody starting to talk — so the device
can answer *itself*. Device-observed 2026-09-07 on `intern-v2-6286` at 70 %
volume: four unprompted replies in 35 s with nobody in the room ("What's up?",
"I'm here. What can I do for you?"), each one re-arming the K hold, and one
logged `barge-in: model interrupted by the user` with no user present.

`HAL_LIVE_MAX_UNPROMPTED_REPLIES` bounds the damage — it ends a session once the
model has produced that many replies in a row with nothing from the user — but
it is a backstop, not a cure. It counts replies rather than seconds precisely so
that a single answer running for minutes is never mistaken for a loop. The cure is `cancelled` mode over a canceller good enough to earn it,
so the model always hears the true room with no artificial transitions. Lower
speaker volume makes it markedly less likely in the meantime.

### Ending a session

A session ends after `HAL_LIVE_IDLE_HANGUP_S` (K, default 15 s) with **no action
from the user**, and the mic goes straight back to the VAD, which opens a new
session on the next real speech. Two details make this behave:

- **The model's reply is not user action**, but the clock is *held* while the
  device is speaking, so a long answer is never cut off mid-sentence. The window
  runs from whichever came later: the user's last words, or the moment the
  device stopped talking — which is exactly when it becomes the user's turn.
- **RMS alone cannot carry this clock.** In a noisy room the floor sits above
  `HAL_VAD_THRESHOLD`, so every frame reads as "the user is talking" and the
  session never hangs up. Device-observed 2026-09-07 on `intern-v2-6286`: a
  ~10500 noise floor against a threshold of 500 held one session open
  indefinitely, and because a live session owns the mic, the VAD never ran
  again — the device went deaf until restart. So RMS is the cheap first gate and
  **Silero confirms** before the clock is refreshed, batched over
  `HAL_SILENCE_VAD_WINDOW_FRAMES`, exactly as the turn path's silence clock does.

`HAL_LIVE_MAX_S` is the backstop for a room so noisy that even Silero keeps
agreeing.

Read the counters in the session-END log line: `substituted` at ~100 % of
`during_playback` is `mute` working as designed.

## Turn flow (in `voice_service.py`)

1. **Construct + start.** `RealtimeOrchestrator(gateway=AGENT_GATEWAY)` is built;
   `start()` runs in a daemon thread (`realtime-start`) when `HAL_REALTIME_ENABLED`.
   TTS `on_speak_end` is hooked to feed spoken text back as `[TTS HISTORY]`,
   but **only when that speech opted in** (`TTSService.realtime_feedback`, set by
   the `realtime_feedback` flag on `/voice/speak[-queue]`). Only the agentic
   runtime's actual reply opts in — os-server sends it via `hal.SpeakReply` /
   `hal.SpeakQueueReply` (which `SendToHALTTS` / `SendToHALTTSQueue` use).
   The same opted-in fragments are appended to realtime memory, so a new Gemini
   session retains a main-agent answer rather than relying on the old socket.
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

   Both post-capture rows are additionally gated on the turn **not** being noise.
   They run after the noise guard has already classified the capture, so an
   empty-STT non-speech turn opens nothing: no `[TURN CONTEXT]`, no audio, and no
   replacement session. Skipping the send is what makes the skip-commit path free
   — otherwise the turn's whole buffer entered (and was billed by) an open
   activity that the very next step discarded. Sessions opened *earlier* in the
   capture (always-listening) have already streamed audio and are still discarded.

   In always-listening mode the speaker-ID prepass (`identify_and_decorate`, run
   **once** at session end) resolves the voice speaker *after* the context already
   went out with the face name. HAL then sends a `[TURN CONTEXT UPDATE]` correction
   naming the real speaker — still **before** `commit_audio()`, so it is part of the
   same turn. A short transcript in the AI-rejection ambiguity range defers this
   external embedding call until after realtime decides; an explicit rejection
   avoids the call entirely, while every non-rejected downstream turn still gets
   the same one-time identity result. It is skipped when the context already carried
   the right name, or when the turn is noise.

   **The prepass no longer blocks the model.** It used to run inline, strictly
   before the realtime turn opened, so its whole external round trip sat between
   the user falling silent and the model receiving the utterance — measured on
   lamp-0c89 (03/09/2026): 1.49s of a 3.0s gap, the rest being the Gemini
   pre-turn reconnect. It now runs on its own thread while that reconnect
   happens, and the turn joins it (`SPEAKER_PREPASS_JOIN_S`, `HAL_SPEAKER_PREPASS_JOIN_S`,
   default 2.0s) at the first point the name is needed. The wait is a ceiling,
   not a delay: a prepass that finished during the reconnect costs nothing, and
   reaching the ceiling only means this turn's context goes out with the speaker
   unresolved — exactly what the always-listening row above already does, and the
   `[TURN CONTEXT UPDATE]` correction still covers it. The deferred short-transcript
   path is unchanged.

   **The verdict is cached.** Recognition used to run once per turn, every turn:
   a ten-turn conversation paid for ten external calls to be told the same name.
   `SpeakerDecorator` now reuses the last verdict for `SPEAKER_ID_CACHE_S`
   (`HAL_SPEAKER_ID_CACHE_S`, default 90s), and for `SPEAKER_ID_CACHE_FOLLOWUP_S`
   (default 300s) inside a wake-word follow-up window, where the turns are one
   conversation by definition. **Unknown is cached too** — an utterance the
   recognizer could not place is the case most likely to repeat, and retrying it
   every turn pays the full latency for the same non-answer; the only thing lost
   on a cached unknown is the enrolment WAV path for that turn. `POST
   /speaker/current-user/reset` clears the cache along with the current voice
   user, since it is the same presence state one layer down.

   **The replaced session is closed in the background.** A pre-turn rebuild
   swaps in a fresh session and then tears the old one down; doing that inline
   made the turn wait for `aclose()` plus the IO-thread join — 0.79s measured
   between the new session opening and this turn's `[TURN CONTEXT]` going out
   (lamp-0c89, 03/09/2026). Nothing needs the old socket to be closed before the
   model can hear the user, so the close runs on its own thread (failures are
   still logged; a thread that cannot start falls back to closing inline rather
   than leaking the socket).

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
   one dedicated realtime filler from its cache — a quiet non-lexical thought
   such as "Mm...", distinct from the main-agent's opening acknowledgement.
   Os-server owns the phrase pools, language, and WAV cache. Whether this fires
   on every turn or only on slow ones is a
   property of the model, and the default assumes a fast one: a chit-chat reply
   arriving in ~1 s never reaches the timer, while a turn grounded with Google
   Search does. Measure before trusting that on a given body — on `lamp-0c89`
   (26/08/2026, `gemini-3.1-flash-live-preview` behind the campaign-api proxy)
   no turn reached its first sentence in under 3.0 s (median 4.0 s, n=31), so
   the filler is the only thing the user hears early and the lamp lowers the
   delay to 0.5 s in its device `.env`. Set it from measured
   time-to-first-sentence, not from the default. **It is not armed for a short transcript in the noise-guard
   ambiguity range** (up to `HAL_REALTIME_NOISE_GUARD_MAX_WORDS`, default 3):
   the model may explicitly reject `o`, `you.`, or `Yeah.` shortly after commit,
   and an early filler would turn that silent rejection into an audible nuisance.
   The filler is interruptible, so the model's first sentence
   cuts it off; every exit path (reply, delegate, empty turn, exception)
   cancels the timer, and delegate cancels explicitly because the main-agent
   hop that follows fires its own filler. `0` disables.

   Speaking the filler is TTS, so it stops the thinking pulse and runs the
   speaking wave. To keep the rest of the wait visible, the cue marks the
   strip as its own (`app_state._thinking_cue_active`): the LED restore that
   follows TTS repaints the thinking pulse instead of settling on the user
   state. The flag is dropped when the cue clears and by any other emotion
   coming through `POST /emotion`, so an expressed emotion is never stomped.

   A **delegated** turn keeps the cue on purpose — the main-agent hop that
   follows is the longer wait, and its own hook re-fires `thinking` anyway. A
   turn that raises does *not* count as that handover (nothing in HAL is still
   driving the face), so the exception path clears the cue before falling
   through to the OS server.

   Because `thinking` is only ever ended by the emotion the reply expresses, a
   turn that produces none — a delegate the agent answers without an emotion
   marker, a forward that never happens — used to leave the face and, through
   `_thinking_cue_active`, every later LED restore stuck on the pulse until the
   user spoke again. Two things end it now.

   **The reply finishing is the end of the wait.** `_on_tts_speak_end`
   (`hal/app_state.py`) clears `thinking` when TTS ends, gated on
   `tts_service.realtime_feedback` — the flag only the agentic runtime's own
   reply sets. Dead-air fillers, mumble and system notices leave it False, so
   the TTS that plays *during* a wait (exactly what the cue flag exists to
   survive) does not end the cue. This covers the common case at the right
   moment: the face is correct the instant the device stops talking, whether or
   not the agent bothered with an emotion marker.

   **The watchdog is the net for turns that never speak.** `POST /emotion` arms
   a last-resort timer whenever
   the emotion is `thinking`: after `HAL_EMOTION_THINKING_RESET_S` (default
   25 s, `0` disables) of *continuous* thinking it drops the cue flag, expresses
   `idle`, and restores the user's LED state. Any other emotion cancels the
   timer; a fresh `thinking` re-arms it. The window clears the longest real hold
   measured on device (realtime replies clear in 0.4-8.6 s; a delegated
   event-forwarded → assistant-turn-done runs 6-22 s), so it cannot blink idle
   in the middle of a live turn.
5. **Consume.** `for output in stream_output()`:
   - `TextOutput` → sentences are flushed to TTS (`speak` / `speak_queue`).
     By default (`HAL_REALTIME_FIRST_CHUNK_MAX_CHARS=0`), the first complete
     sentence is spoken immediately; later sentences continue through the
     pre-synthesis queue. This does not wait for the whole reply. A positive
     value opts into splitting the first incomplete sentence at its last clause
     boundary (`,` `;` `:` `—`), or at a word break beyond that character limit.
     Complete sentences bypass this early splitter. It preserves square-bracket
     voice tags, including during whitespace fallback, and does not split at
     numeric commas/colons or URL colons. A candidate needs at least 8 visible
     characters outside voice tags. Early splitting can reduce initial waiting
     but still create gaps between synthesis requests; it is an opt-in tradeoff.
     Native audio is unaffected and continues to stream frame by frame.
     If `speak` returns busy (another non-interruptible TTS holds the
     speaker, e.g. an ambient nudge), the sentence falls back to
     `speak_queue` so the reply plays after it instead of being lost.
     Queued agent speech is turn-aware: each entry carries a `turn_id` and
     monotonically increasing `turn_seq`.
     Accepting a newer run stops the active older utterance and removes its
     pending entries, so the user hears the reply that is relevant now. A
     delayed request from the superseded run is dropped rather than rejoining
     the queue. `POST /tts/stop` likewise cancels both active playback and
     every pending entry.
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

### STT model and language

`stt_language` selects the persisted `stt_model`: English uses
`flux-general-en`; Vietnamese and the other supported non-English languages use
`nova-3-general` with the selected BCP-47 language code. That pair is passed to
the AutonomousSTT proxy, including a healthwatch voice-pipeline restart. This
makes a saved Vietnamese configuration effective after a proxy restart; it does
not claim that one model provides arbitrary Vietnamese-English code-switching.

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
| `wakeword` | ROBOT.md `voice.wakeword` on a fresh config, else `false` | Top-level config-file wake-word gate. When true, a matching interim transcript is provisional only: HAL commits buffered audio to realtime or forwards a command only after an STT **final** result confirms a configured wake phrase. The transcript is split into sentences (`.` `!` `?`) and the phrase is accepted at the start **or the end** of any sentence; mid-sentence occurrences are rejected. The confirmation re-checks the assembled, still-punctuated transcript so the `\w+`-only merge step cannot retract a gate a partial opened. If that exact re-check fails but a partial had already matched exactly, the name alone may differ by one letter and the gate still confirms: STT rewrites its own hypothesis in the final, and on lamp-0c89 (04/09/2026) the partial `hello lamp` came back as `Hello, lamb.`, which dropped the whole turn — no realtime turn, no thinking cue, and the question fell through to the much slower main agent. The prefix (`hello`, `hey`, …) must still match exactly and the loose rule can never OPEN a gate, only confirm one an exact partial opened, so a near-miss word in ambient speech still wakes nothing. It is logged as `Wake-word confirmed with a one-letter STT slip` so the rate stays countable — many of them means the STT boost terms are not doing their job. The supported prefixes are `hello`, `hey`, `hi`, `alo`, `okay`, `ok`, and `wake up`, applied to the permanent common alias (`hey autonomous`), device type (`hey lamp`), and current agent name (`hey Luna`). A runtime rename updates only the agent-name aliases. Bare names and other prefixes do not arm the gate. A rejected utterance is discarded and its transient listening LED restores to the normal resting state; it never leaves the persistent idle effect active. A confirmed turn opens the follow-up focus window; turns in that window are forwarded as `voice_followup` without another phrase. Every authorized turn dispatches to os-server: a spoken realtime reply becomes a silent `voice_agent_handled` sync event; unavailable, silent, failed, or delegated realtime follows the normal path. If realtime is disabled, the confirmed final transcript follows the normal os-server path. Missing/false preserves the pre-gate always-listening flow unchanged. On a config.json os-server creates, the initial value comes from the body's `voice.wakeword` (see Wake-word gate above); a config loaded without the key stays `false`. HAL restarts after a local Settings save or MQTT `wakeword.gate`. |
| `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S` | `20` | Idle seconds for the short post-command focus window. Each accepted `voice_command` or `voice_followup` refreshes it. `0` disables follow-ups and requires a wake phrase for every mic session. Ignored when `wakeword` is false. |
| `HAL_ENDPOINT_SILENCE_S` | `0.8` | Silence needed after STT final arrival, only while `final_ts >= last_confirmed_speech`. Continued confirmed speech after that final restores the 2.5s fallback until a new final arrives. `0` disables the short clock, leaving `HAL_SILENCE_TIMEOUT`. |
| `HAL_SILENCE_VAD_ENABLED` | `true` | Require Silero to confirm speech before the end-of-turn silence clock is refreshed. RMS remains the cheap pre-gate; set `false` to fall back to pure-RMS silence detection. |
| `HAL_SILENCE_VAD_WINDOW_FRAMES` | `3` | Number of frames batched per Silero run for that check — Silero costs ~20 ms/frame on ARM and its LSTM needs more than one 64 ms frame to settle. |
| `HAL_REALTIME_PROVIDER` | `gemini` | `none` \| `gemini` \| `openai` \| `qwen` |
| `HAL_REALTIME_TURN_DETECTION` | `off` | `server_vad` \| `semantic_vad` \| `off` (Gemini: off = manual activity detection) |
| `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` | `8.0` | Max seconds `receive()` waits for the next output event before ending a silent turn (fallback to main agent) |
| `HAL_REALTIME_GROUNDING_DEBUG` | `false` | Dump every field of Gemini's `grounding_metadata` verbatim, once per grounded turn (`grounding_chunks`, `grounding_supports`, `search_entry_point`, …). Diagnostic only and verbose; it exists to tell a turn whose search genuinely returned nothing from one whose payload was stripped in transit. Measured on lamp-0c89 04/09/2026 over four grounded turns, the payload always arrived complete, so a `chunks=0` turn means the model did not ground that answer. |
| `HAL_REALTIME_TURN_MAX_SILENCE_S` | `20.0` | Ceiling on how long one turn may stay silent while the server keeps sending messages. `receive()` extends a turn past `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` only while inbound traffic proves the model is still working (a grounded search emits nothing until it returns); this stops a server that chatters without ever producing output from hanging the turn. `0` disables the keep-alive and restores the plain gap watchdog. |
| `HAL_REALTIME_LOOK_RECV_TIMEOUT_S` | `20.0` | Silent-turn watchdog used instead of the default for turns where a `look` fired (per-turn, via `extend_recv_timeout()`). Gemini's forced thinking over a text-dense frame can stay silent >8 s right before the answer — the default watchdog was killing those turns. Raising it delays the look-frame handoff, so keep `HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S` above it |
| `HAL_REALTIME_REQUIRE_TRANSCRIPT` | `true` | Never commit an empty-STT turn to the model. A final transcript containing only punctuation or symbols (for example `.`) is normalized to empty before gaze, speaker-ID, realtime, dispatch, or follow-up refresh; it cannot create a `voice_followup`. Real speech that nova-3 missed (short utterances) is voiced and passes the VAD/Silero guards, so committing its raw audio makes the model invent a reply to silence (a generic greeting, often with a name nobody said). When `true`, any empty-STT turn is dropped regardless of duration/voicing — silence beats a wrong reply. Set `false` to fall back to the Silero-gated audio-only path below. |
| `HAL_REALTIME_AI_REJECT_FILTER` | `true` | Registers `reject_turn` and enables the isolated `should_drop_realtime_rejection()` policy gate. An explicit tool call drops a transcript before OS dispatch; a silent model completion, timeout, or error still falls back to the main agent. The separate deterministic noise guard is also terminal for audio it already classified as non-speech. Set `false` to disable this experimental AI filter without changing the rest of realtime routing. |
| `HAL_REALTIME_FIRST_CHUNK_MAX_CHARS` | `0` | Default: speak the first complete sentence immediately and pre-synthesize later sentences in the queue, without waiting for the whole reply. Positive values opt into first-clause splitting, with a word-break fallback beyond this limit; complete sentences bypass the splitter. Keeps bracketed voice tags intact (also at whitespace fallback), skips numeric commas/colons and URL colons, and requires 8 visible characters outside tags. Early splitting may leave gaps between synthesis requests. |
| `HAL_REALTIME_MIN_COMMIT_DURATION_S` | `0.8` | Sessions shorter than this with no STT transcript are treated as VAD noise and not committed to the model. Only consulted when `HAL_REALTIME_REQUIRE_TRANSCRIPT=false`. |
| `HAL_REALTIME_NOISE_GUARD_MAX_WORDS` | `3` | Extends the Silero voiced-ratio guard to turns that DO have a transcript, up to this many words. STT invents a short filler out of room noise and reports full confidence for it, so such a turn used to bypass every guard (they all only ran on an empty transcript) and commit pure noise to the model. A transcript of at most this many words is re-checked against `HAL_REALTIME_NOISE_SPEECH_RATIO` and dropped when the audio was never voiced; a real short command is voiced and still commits. The ratio is measured over the voiced SPAN — first to last voiced chunk — not the whole buffer, because a capture carries VAD pre-roll at the front and a 200ms tail at the back, and that fixed padding dilutes a short utterance far more than a long one. Measuring the whole buffer dropped a real `Yes, that's right.` at 0.500 (`peak=1.000`), inverting the guard's purpose against the very turns it screens. Sustained noise still fails, since its voiced chunks are sparse within the span too. Longer transcripts are never re-checked, so the floor can't silence a real utterance. `0` disables. |
| `HAL_REALTIME_SESSION_IDLE_RESET_S` | `240` | Cost control: when a turn arrives after this many seconds of silence, recycle (rebuild) the session **after** that turn so the next turn drops the per-turn context the provider re-bills on a long-lived session. A post-pause turn is effectively a new conversation; long-term continuity survives via the reloaded `summary.md`. For native-audio Gemini, this is skipped when a successful pre-turn recycle already made the same idle gap fresh. `0` disables. Reuses the zombie-recovery rebuild path. |
| `HAL_GEMINI_SESSION_RESUMPTION` | `false` | Resume the same Gemini session across reconnects. OFF by default — the `campaign-api` proxy doesn't forward the resumption handshake, so resuming through it yields a zombie session (cold reconnects work). Enable only against an endpoint that supports it. |
| `HAL_GEMINI_IDLE_PARK_S` | `45` | Gemini idle parking: close the session's transport after this many seconds without turn activity, so the server never closes it with WS `1008` (which the backend logs as an error and alerts on). The orchestrator stays `available` while parked; the next turn's `prepare_turn()` reconnects synchronously before streaming audio. Must stay below the shortest observed idle death (86 s). `0` disables. |
| `HAL_GEMINI_PRE_TURN_RECYCLE_S` | `60` | Gemini transport guard: when a new spoken turn starts after this much idle time, rebuild the Gemini session **before** streaming pre-roll/audio so the turn does not hit a proxy/SDK idle-dead socket. `0` disables. A successful pre-turn recycle suppresses the generic post-turn idle recycle for that same turn, so one idle gap creates at most one cost/transport rebuild. |
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
| `HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S` | `45` | Max age of a `look` frame still handed off to the main agent on a delegate/timeout fallback so it reuses the image instead of re-snapshotting. **Must stay above `HAL_REALTIME_LOOK_RECV_TIMEOUT_S` plus dispatch time** — the timeout fallback only fires after that watchdog expires, so equal values expire every frame (both were `20` from 2026-07-06 to 2026-08-24 and the handoff never once fired). `0` disables the age guard (frame is still cleared per-turn). |
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
| `HAL_REALTIME_SUMMARIZER_RETRIES` | `2` | Extra attempts per summarize; `0` disables |
| `HAL_REALTIME_SUMMARIZER_RETRY_BACKOFF_S` | `1.5` | Wait before the first retry, doubled each time |

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
| `../voice/voice_service.py` | Integration: streams mic audio, consumes output, routes delegate/handled. Live mode: `_live_decision` / `_live_session` / `_live_out_pump` / `_live_uplink_frame` |
| `../voice/aec.py` | WebRTC AEC3 on the mic path; reference tapped at the TTS output stream (all providers) |

### Buddy agent completion events

Managed desktop sessions report completion/attention/error through the standard sensing route as `buddy.agent.<session_id>`. These passive events queue while the agent or speaker is busy, with distinct session types preserving parallel notifications; the existing sleep and voice privacy policies remain in force. The lamp uses the `agent-management` skill and explicit project/session IDs for follow-ups. Summaries are untrusted result data, not tool authorization. Delivery is best effort; inspecting `agent.session` remains the recovery path.

### Desktop task follow-ups in realtime delegation

All four realtime prompt variants and the shared `delegate_to_main` description explicitly route native desktop actions and clear answers, corrections, or stop requests for a known pending main-agent task to the main agent, with blank realtime speech. The delegated message contains only the current user's faithfully understood words and supplied parameters, preserving every clause. Known task context is used internally to decide routing; it is not appended or retold because the main agent already owns the conversation. A brief reply such as “this weekend, two people” can continue the preceding lodging-search clarification; it must not be discarded solely for lacking an action verb or expanded into invented dates.

Recent spoken main-agent questions in `[TTS HISTORY]` can supply the context needed to interpret that follow-up, while the existing no-repetition rule remains. `[TTS HISTORY, not spoken]` does not establish that the user heard or answered the question. Background-speech and addressed-to-device checks remain in force. This change uses the existing realtime handoff/reply history; it adds no structured pending-task store and does not itself prove live voice routing success or remove provider-specific context-delivery limits.

Delegation must preserve named applications and dictated text, not merely the general topic. For example, “Ghi vào Notes là chiều mua sữa” must retain Notes and the full text “chiều mua sữa”; reducing it to a generic milk reminder loses both the target and content. The prompt variants and tool description now state this explicitly. A synthetic-audio observation showed that loss in a delegated message; without an input transcript it does not isolate audio recognition from summarization, and the wording change still requires behavioral verification.

The current request or follow-up is forwarded in the language the user just spoke, without commentary or a summary of previous turns. Translating it to English can incorrectly switch the main agent’s reply language because the delegated instruction is its primary input.

A subsequent isolated Gemini 3.1 Live synthetic-audio comparison reused identical PCM clips across the baseline and final prompt/tool definitions. The final delegate messages were “Ghi vào Notes là sáng mai tưới cây.”, “Mở Airbnb tìm chỗ ở Đà Nẵng giúp mình.”, and the follow-up “cuối tuần này hai người”. The baseline had changed the Notes request into “Remember to water the plants tomorrow morning.”, losing the named app and changing language. The Airbnb follow-up used the same provider session after a controlled `[TTS HISTORY]` clarification; the previous server completion boundary and new audio commit were confirmed. This establishes the observed delegation behavior for those synthetic clips, not microphone/wake-word performance, an actual main-agent clarification, or main-agent/desktop end-to-end completion. The isolated final result was recorded at `/tmp/buddy-rt-final/result.json` on the test device; no production prompt or service was changed by that evaluation.
