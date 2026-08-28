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
- **Explicitly rejects** a high-confidence non-user turn by calling
  `reject_turn`, which drops the turn before the main agent sees its STT text.
  This is deliberately different from a silent completion: silence, timeout,
  and transport failure still use the normal main-agent fallback.

The `delegate_to_main` tool is registered automatically by the orchestrator
(`orchestrator.py`, `DELEGATE_TOOL`).

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
turn can omit the wake phrase and is sent as `voice_followup`. A follow-up has
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
says "one moment" about the old one and goes quiet. The behaviour is opt-in per body: set `OS_REALTIME_AUTO_MUTE=1` in the body's
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
closes, clears, or mutes user microphone capture, so barge-in remains available.

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

**Off by default** (`HAL_AEC_ENABLED=false`); the lamp image opts in via its
device `.env`. It needs the
`aec-audio-processing` binding, which is **not** a base hal dependency — PyPI
ships no Linux wheels for it, so a device builds it from source. It lives behind
the `aec` extra (`uv sync --extra aec`), deliberately kept out of `dependencies`
and out of `hardware`: the build needs meson/ninja, which the lamp image does
not install, so a hard dep would break both the image build and
`software-update hal` for a feature that is off by default. When the import
fails, `configure()` logs once and every entry point becomes a no-op; the voice
path behaves exactly as before, so the default is safe on a device without the
binding. It does, however, also switch **barge-in** on (see below), and that is
not a no-op.

| Env | Default | Meaning |
|-----|---------|---------|
| `HAL_AEC_ENABLED` | `false` | Master switch. Also the default for `HAL_BARGE_IN_ENABLED` |
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
own speech rather than only in the legacy barge-in monitor. The reverb gate is
deliberately left uncancelled so its timing is unchanged.

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

`aec.uncancelled()` reports whether the frame just read went through *without*
real cancellation — reference underrun, bypassed stream, or mic overrun. Barge-in
gates on it so it cannot decide on raw echo. Note what it does **not** say:
it reports whether a reference *arrived*, not whether cancellation *worked*, so
a frame with 0.9 dB of ERLE still counts as cancelled.

### Barge-in: level cannot separate echo from a person

The residual that survives cancellation is loud enough to look like a user
interrupting, and it **is** speech, so neither the level gate nor the speech
classifier can reject it. Measured in a silent room, echo ceiling (the
`drain peak RMS=` each reply logs) against real interruptions:

| Speaker volume | Mixer | Echo ceiling | Real interruption |
|---|---|---|---|
| 25 % (`lamp-ee17`) | −45 dB | 9804 | 8027 |
| 40 % (`lamp-0c89`) | −36 dB | 9969 | 6956–8027 |
| 65 % (`lamp-0c89`) | −21 dB | 13560 | 6956 |

The echo ceiling sits **above** the real interruptions at every volume, so a
threshold below it self-interrupts and one above it misses ordinary speech.
Lowering the speaker is not a workaround either: 24 dB of mixer range moved the
ceiling by under 3 dB, because the coupling is not dominated by the airborne
path. Do not spend time re-tuning `HAL_BARGE_IN_RMS_THRESHOLD` — no value works.

What does separate them is `aec.echo_envelope_match()`
(`HAL_BARGE_IN_ECHO_MATCH`, default `0.65`), which runs third, only on
candidates that already passed level and speech. It works in three steps on
log-energy envelopes at 8 ms resolution, taken from the **raw** mic:

1. **Align.** Cross-correlate the candidate window against the retained
   reference and take the best lag. Correlation only locates the window — it is
   not the verdict, because during double talk the raw mic carries the echo well
   above the person and correlates highly whatever they say.
2. **Subtract.** Remove the aligned reference plus the coupling gain (median
   offset), keeping only frames where the reply is actually loud. In the gaps
   between its words the reference predicts near-silence, so room noise there
   would read as a huge unexplained excess.
3. **Measure the skew, not the size.** Echo never fits perfectly — reverb, mic
   noise and a coupling that is not a clean scaling leave a couple of dB either
   way. A person is one-sided: they can only *add* energy. So a top tail that
   outruns the bottom tail is someone else in the room, and a symmetric
   residual is echo however large it is.

Measured on `lamp-0c89`, speaker 40 %, labelled against the transcript that
followed each candidate:

| | Residual skew |
|---|---|
| Echo, silent room (15 windows) | −2.8 … +2.1 dB |
| Echo, mixed run (~40 windows) | −50.0 … **+4.8** dB |
| Confirmed real interruption | **+8.4** … +40.4 dB |

The effective cut sits near 6.6 dB, inside that gap and biased towards missing a
quiet interruption over cutting the reply off. Verification run: 12 replies into
a silent room fired **zero** barge-ins.

Two things that were tried and rejected, both recorded in the code so they are
not retried: comparing the **cancelled** signal instead of the raw one (the APM
is a time-varying gain and eats the contour — echo scored 0.42–0.45 and leaked
through), and the textbook double-talk variable σ_e/σ_d, still logged as `supp`
(echo 0.3–10.1 dB against person 0.1–8.2 dB — fully overlapping, because ERLE
here is 6 dB at best and swings per frame).

`None` means *unknown*, not clean — too little reference, or a fit pinned at the
oldest edge of the retained reference, which means the true alignment fell off
the end. The caller treats it as "do not fire": the speaker is audibly playing
at that moment, which is the one situation where unknown has to mean no.

`EchoReference` keeps a 2 s **history** alongside the FIFO, and the canceller
keeps the same span of raw mic. The FIFO is drained by `process()`, so by the
time a candidate is judged the reference for its frames is already gone; 800 ms
was not enough because the TTS tap fires when ALSA *accepts* audio and runs
ahead of playback in bursts.

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
   sound alike. Whether this fires on every turn or only on slow ones is a
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
| `wakeword` | ROBOT.md `voice.wakeword` on a fresh config, else `false` | Top-level config-file wake-word gate. When true, a matching interim transcript is provisional only: HAL commits buffered audio to realtime or forwards a command only after an STT **final** result confirms a configured wake phrase. The transcript is split into sentences (`.` `!` `?`) and the phrase is accepted at the start **or the end** of any sentence; mid-sentence occurrences are rejected. The confirmation re-checks the assembled, still-punctuated transcript so the `\w+`-only merge step cannot retract a gate a partial opened. The supported prefixes are `hello`, `hey`, `hi`, `alo`, `okay`, `ok`, and `wake up`, applied to the permanent common alias (`hey autonomous`), device type (`hey lamp`), and current agent name (`hey Luna`). A runtime rename updates only the agent-name aliases. Bare names and other prefixes do not arm the gate. A rejected utterance is discarded and its transient listening LED restores to the normal resting state; it never leaves the persistent idle effect active. A confirmed turn opens the follow-up focus window; turns in that window are forwarded as `voice_followup` without another phrase. Every authorized turn dispatches to os-server: a spoken realtime reply becomes a silent `voice_agent_handled` sync event; unavailable, silent, failed, or delegated realtime follows the normal path. If realtime is disabled, the confirmed final transcript follows the normal os-server path. Missing/false preserves the pre-gate always-listening flow unchanged. On a config.json os-server creates, the initial value comes from the body's `voice.wakeword` (see Wake-word gate above); a config loaded without the key stays `false`. HAL restarts after a local Settings save or MQTT `wakeword.gate`. |
| `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S` | `20` | Idle seconds for the short post-command focus window. Each accepted `voice_command` or `voice_followup` refreshes it. `0` disables follow-ups and requires a wake phrase for every mic session. Ignored when `wakeword` is false. |
| `HAL_SILENCE_VAD_ENABLED` | `true` | Require Silero to confirm speech before the end-of-turn silence clock is refreshed. RMS remains the cheap pre-gate; set `false` to fall back to pure-RMS silence detection. |
| `HAL_SILENCE_VAD_WINDOW_FRAMES` | `3` | Number of frames batched per Silero run for that check — Silero costs ~20 ms/frame on ARM and its LSTM needs more than one 64 ms frame to settle. |
| `HAL_REALTIME_PROVIDER` | `gemini` | `none` \| `gemini` \| `openai` \| `qwen` |
| `HAL_REALTIME_TURN_DETECTION` | `off` | `server_vad` \| `semantic_vad` \| `off` (Gemini: off = manual activity detection) |
| `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` | `8.0` | Max seconds `receive()` waits for the next output event before ending a silent turn (fallback to main agent) |
| `HAL_REALTIME_LOOK_RECV_TIMEOUT_S` | `20.0` | Silent-turn watchdog used instead of the default for turns where a `look` fired (per-turn, via `extend_recv_timeout()`). Gemini's forced thinking over a text-dense frame can stay silent >8 s right before the answer — the default watchdog was killing those turns. Raising it delays the look-frame handoff, so keep `HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S` above it |
| `HAL_REALTIME_REQUIRE_TRANSCRIPT` | `true` | Never commit an empty-STT turn to the model. A final transcript containing only punctuation or symbols (for example `.`) is normalized to empty before gaze, speaker-ID, realtime, dispatch, or follow-up refresh; it cannot create a `voice_followup`. Real speech that nova-3 missed (short utterances) is voiced and passes the VAD/Silero guards, so committing its raw audio makes the model invent a reply to silence (a generic greeting, often with a name nobody said). When `true`, any empty-STT turn is dropped regardless of duration/voicing — silence beats a wrong reply. Set `false` to fall back to the Silero-gated audio-only path below. |
| `HAL_REALTIME_AI_REJECT_FILTER` | `true` | Registers `reject_turn` and enables the isolated `should_drop_realtime_rejection()` policy gate. An explicit tool call drops a transcript before OS dispatch; a silent model completion, timeout, or error still falls back to the main agent. The separate deterministic noise guard is also terminal for audio it already classified as non-speech. Set `false` to disable this experimental AI filter without changing the rest of realtime routing. |
| `HAL_REALTIME_MIN_COMMIT_DURATION_S` | `0.8` | Sessions shorter than this with no STT transcript are treated as VAD noise and not committed to the model. Only consulted when `HAL_REALTIME_REQUIRE_TRANSCRIPT=false`. |
| `HAL_REALTIME_NOISE_GUARD_MAX_WORDS` | `3` | Extends the Silero voiced-ratio guard to turns that DO have a transcript, up to this many words. STT invents a short filler out of room noise and reports full confidence for it, so such a turn used to bypass every guard (they all only ran on an empty transcript) and commit pure noise to the model. A transcript of at most this many words is re-checked against `HAL_REALTIME_NOISE_SPEECH_RATIO` and dropped when the audio was never voiced; a real short command is voiced and still commits. The ratio is measured over the voiced SPAN — first to last voiced chunk — not the whole buffer, because a capture carries VAD pre-roll at the front and a 200ms tail at the back, and that fixed padding dilutes a short utterance far more than a long one. Measuring the whole buffer dropped a real `Yes, that's right.` at 0.500 (`peak=1.000`), inverting the guard's purpose against the very turns it screens. Sustained noise still fails, since its voiced chunks are sparse within the span too. Longer transcripts are never re-checked, so the floor can't silence a real utterance. `0` disables. |
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
| `../voice/aec.py` | WebRTC AEC3 on the mic path; reference tapped at the TTS output stream (all providers) |
