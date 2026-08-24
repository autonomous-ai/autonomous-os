# Physical Controls — GPIO Button + TTP223 Touchpad

Lamp has two physical input devices the user can touch directly. They share the same action library (`hal/drivers/button_actions.py`) so any gesture mapped to "single click" behaves identically whether it came from the mechanical button or the capacitive touchpad.

## Why two devices

| Device | Role | Where |
|---|---|---|
| **GPIO button** | One mechanical button. Used for decisive actions including destructive ones (reboot / shutdown / factory-reset). The mechanical feel and long-hold detection make accidental destructive actions unlikely. | Both Pi 4/5 and OrangePi sun60 |
| **TTP223 capacitive touchpad** | Four touch pads arranged as a "dog head" surface for petting + soft stop/unmute. No destructive gestures because the IC's FastMode prevents reliable hold detection. | OrangePi sun60 only (4 Pro / A733) |

## Wiring

| Device | Pi 4/5 | OrangePi sun60 |
|---|---|---|
| GPIO button | gpiochip0 BCM 17 (pull-up, active-LOW) | gpiochip1 line 9 (pull-up, active-LOW) |
| TTP223 | not wired | gpiochip0 lines 96 / 97 / 98 / 99 (named S1–S4), pull-down, active-HIGH |

Board detection in both handlers reads `/proc/device-tree/model`:
- `"sun60iw2"` → OrangePi 4 Pro / A733
- `"raspberry pi 5"` → Pi 5
- `"raspberry pi 4"` → Pi 4
- else → unknown, both handlers skip claiming GPIO lines

## Gesture map

| Gesture | GPIO button | TTP223 touchpad |
|---|---|---|
| **1 tap** | Stop active object tracking, then stop speaker / unmute mic + speaker + ack chime (~120 ms ping) — all fire immediately on release (no click-window wait); the "Listening" cue plays once the 0.4 s click window resolves | Same after the 1.2 s tap-vs-pet decision resolves — active tracking stops, then the mic/speaker action and cue run. The initial touch still stops in-flight TTS and plays its ack chime immediately. |
| **2 taps** (≤ 0.4 s apart, button) / (≤ 1.2 s apart, TTP223) | Nothing beyond the single-click already fired on tap 1 (panic-click guard) | Pet response — TTS picks a random phrase from the language pool |
| **3 taps** (≤ 0.4 s apart, button) | Reboot OS (TTS announce → `sudo reboot`) | n/a — TTP223 stops at 2 (any further taps absorbed by cooldown) |
| **Hold 2–5 s, then release** | Speak the localized sleep announcement, then enter `sleepy`: LED off, camera/mic/speaker off; servo releases after 1 s. LED blinks sleepy purple while held. | n/a — TTP223 hardware cannot reliably hold (see "FastMode" below) |
| **Hold 5–10 s, then release** | Shutdown OS (TTS announce → release servos → `sudo shutdown -h now`). LED blinks red while armed. | n/a — TTP223 hardware cannot reliably hold (see "FastMode" below) |
| **Hold 10 s+, then release** | Factory-reset: wipe device state + reboot into AP setup (TTS announce → release servos → POST `/api/system/factory-reset` on the OS server). LED goes solid red while armed. | n/a |

Hold gestures are intentionally only on the GPIO button because the mechanical button gives unambiguous evidence of intent. The sleep and destructive hold tiers **commit on release, not on a timer firing while held**. The destructive tiers escalate from shutdown to factory-reset after 10 s (see "GPIO button detection" below).

## Interrupting Lamp while it speaks (barge-in)

The 1-tap gesture is Lamp's primary **barge-in and attention-cancel mechanism**: it first stops any active object-tracking session, then tap top of Lamp (touchpad) or press the GPIO button once during an in-flight TTS to cancel the current utterance mid-word, stop any music, and unmute the mic so Lamp listens for the next thing the user says. A user/scene speaker mute is also relaxed (unless a voice enrollment is recording) so the cue and the reply are audible again. Stopping tracking also works while the hardware mic kill switch is off; it does not wake or unmute the mic. A localized "Listening" cue plays after the cancel when the switch permits the voice action.

When wake word is enabled, the click also **counts as a wake event**: `single_click_action` calls `voice_service.grant_wakeword_focus(source)`, which opens the same follow-up focus window (`HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S`, default 20 s) a spoken wake phrase opens. Without it the device would announce "Listening" and then drop the user's answer for missing the wake phrase. The window is re-checked at dispatch time, not only latched at mic-session start, so a click during an already-open session still authorizes the sentence being spoken. No-op when wake word is off (every utterance already dispatches) or when the follow-up timeout is 0.

### Turning toward the lamp as a third wake trigger

The wake gate has three openers, not two. Alongside the spoken wake phrase and the single click, **turning toward the lamp and speaking** opens the same window (`hal/drivers/tracking/gaze.py`), through the same `voice_service.grant_wakeword_focus(source)` seam the click uses — nothing downstream of the gate changes.

The reason is device shape rather than preference. A desk lamp sits an arm's length from its user and is in view all day, so a wake phrase repeated dozens of times reads as addressing an appliance, and a button press reads as operating one. Between two people the cue is neither: you turn toward someone and speak. Products that popularised "hey <name>" have no camera and sit across a room, so the comparison does not carry.

Two properties decide the implementation:

* **People turn before they speak, never after.** Normally speech reads the watcher's ring buffer (`HAL_GAZE_BUFFER_S`, default 3 s) **backwards** — the same shape as the mic's own pre-roll lookback, which exists so the start of a sentence is not lost. There is one recovery path: if that read has fewer than two usable face samples, VAD asks the watcher to restore the remembered user pose without blocking audio capture. Before dispatching that *same* transcript it checks gaze once more. A head measured facing away does not take this path, so overheard speech still cannot turn the lamp toward a person and open the gate.
* **Presence is not the signal.** The user is visible beside this lamp all day, so "a person is detected" gates nothing, and "a face is detected" barely more — a face turned to a monitor still detects. The gate is on head **orientation**, tight enough to reject the common posture of talking to a colleague with the torso still square to the desk.

Head yaw is derived from the five landmarks `YuNet` already returns (`detect_face_with_landmarks` in `detection.py`): the nose's offset from the eye midpoint, measured along the eye line and normalised by half the inter-ocular distance, is `sin(yaw)` under a pinhole projection. Measuring along the eye line rather than the image x-axis is what keeps a **rolled** head (resting on a hand) from reading as a turned one. No second model is loaded and no extra inference runs; at `HAL_GAZE_SAMPLE_FPS` (default 6) the cost is a rounding error on the 8-core CPU — measured, not assumed: CPU idle went 69.2% to 68.8% with the watcher running.

A landmark outside the frame is not a measurement. `YuNet` reports the five points for a face clipped by a frame edge as readily as for one wholly inside it, and the clipped ones come back off the frame — device-measured with a user sitting straight in front of the lamp, its camera aimed too low: box `[264, -1, 162, 92]` with both eyes at `y = -3.0` and `y = -1.3`. Fed to the yaw those coordinates push the nose ratio past 1, where the clamp turns "not measurable" into exactly `90.0` — indistinguishable from a genuine profile, and counted as a vote **against** facing. That is how a user looking straight at the lamp produced `trail=[90,90,90,90]` and was refused. So a sample whose eyes or nose fall outside the frame is recorded as **unmeasured** — it votes neither way, like a frame with no face at all. Clipped mouth corners are ignored — the angle never reads them.

Detector rows whose box is not a finite number are dropped before any of this. YuNet can return an infinite coordinate for a face leaving the frame — device-observed while tracking, at 1.9% bbox area and 0.29 confidence — and `int()` on it raised `OverflowError`, killing the tracker's detect thread mid-session. Infinity is not a very large face; it is the detector saying nothing usable, so the row goes and the existing "no face this frame" path takes over. The filter runs before the largest / nearest-centre choice, because an infinite width wins any largest-by-area contest and would otherwise hide a perfectly good face behind it.

When several faces are in frame, the one whose head counts is the one **nearest the frame centre** among those at least `HAL_GAZE_MIN_FACE_PX` tall — not the largest. Largest-face would hand the gate to whoever leans in closest, which is the user only by convention; the lamp's own aim is the better prior for which face it is pointed at. With one qualifying face the two rules agree, so this only bites when a second person shares the desk. If nobody clears the size floor the largest face is returned anyway, so the sample still records that somebody is there. Note that the bbox-only tracking path (`_detect_face_yunet`, used by object follow) keeps its own largest-face policy — the two are independent.

| Env var | Default | Tunes |
|---|---|---|
| `HAL_GAZE_WAKE` | `false` | Master switch. Off means today's two openers only. |
| `HAL_GAZE_SHADOW` | `true` | Log the decision without opening the gate. Costs nothing — no turn opens, so no LLM or TTS is spent. |
| `HAL_GAZE_MAX_YAW_DEG` | 25 | Acceptance cone at frame centre. |
| `HAL_GAZE_EDGE_CONE_SCALE` | 1.8 | How much wider the cone grows at the frame edge, where barrel distortion inflates the angle. |
| `HAL_GAZE_MIN_FACE_PX` | 48 | Minimum face height **in pixels**. Below this the landmarks span a few pixels and the yaw is arithmetic on rounding error, so the sample does not vote at all. |
| `HAL_GAZE_WINDOW_S` | 1.5 | Evidence window ending at the moment of speech. |
| `HAL_GAZE_MIN_FACING_RATIO` | 0.6 | Fraction of that window that must have seen a facing head. A ratio, not an unbroken run — per-sample yaw is genuinely noisy. |
| `HAL_GAZE_MIN_SAMPLES` | 2 | Below this there is not enough evidence to decide either way. The loop achieves ~2 samples/s whatever the rate asks for — it is paced by fetching a frame and running the detector — so 3 rejected users the rest of the pipeline agreed were facing the lamp. The `[gaze] sampling at N/s` line counts samples actually RECORDED, and reports separately how many frames were blocked before they could be measured (settling from a servo write, or the detector held by a live look). Counting attempts instead once reported 5.7/s while the buffer held nothing newer than the 1.5 s window — under 1/s of real evidence. |
| `HAL_GAZE_SAMPLE_FPS` | 6 | Sampling rate. The gesture is slow, but the decision is a vote and only measured samples count — at 3 fps a window often held one usable sample, refusing a user facing the lamp dead-on. |
| `HAL_GAZE_BUFFER_S` | 3.0 | Yaw history retained. Must exceed `WINDOW_S`. |
| `HAL_GAZE_COOLDOWN_S` | 5 | Minimum gap between gaze-opened gates, so one conversation cannot open one per sentence. |
| `HAL_GAZE_REPOINT` | `true` | Turn toward the remembered bearing when nobody has been visible. |
| `HAL_GAZE_REPOINT_AFTER_S` | 12 | How long nobody must be visible first. A voice-triggered empty-evidence recovery bypasses this delay, but not the movement cooldown. |
| `HAL_GAZE_REPOINT_COOLDOWN_S` | 60 | At most one turn per this interval, including a voice-triggered recovery. |
| `HAL_GAZE_REPOINT_MIN_CONFIDENCE` | 0.5 | Bearing confidence below which turning is not worth it. |

The lamp image deliberately overrides `HAL_GAZE_MAX_YAW_DEG` to **60°**. This is device calibration, not a generic default: on lamp-0c89 YuNet measured a user looking directly into the camera through glasses at 55.7–59.1°. It does not relax the two-valid-sample minimum or the 60% vote, so a lone frame still cannot open the gate.

Two of these were measured rather than chosen. `MIN_FACE_PX` exists because a device probe found three background colleagues detected at 8-18 px yielding yaw 49 / 20 / 29 — noise — beside the seated user at 78 px whose 90 was correct; the populations do not overlap, so the floor removes the class rather than tuning against it. `MIN_FACING_RATIO` exists because a trail of a stationary user read `[10,15,8,25,36,1,-,90]`, a spread no head performs, so any rule demanding every sample pass would reject them.

Nobody has been visible for a while and the lamp turns: that is `REPOINT`, and it is the only thing in the watcher that moves the body. The idle recording is a loop of absolute poses that swings `base_pitch` about 17 degrees per cycle, so it walks the camera back to the recording's own pose — on a desk, at the keyboard. Parking the remembered pose once would simply be overwritten by the next loop; resting there properly would mean offsetting the whole playback by the bearing, which belongs to motion playback rather than to this feature. So the lamp does what a person does instead: if it cannot see who might be talking to it, it turns to where they usually are, once, then waits.

Thresholds are meant to be chosen from measurement, not guessed — shadow mode exists so a run beside a real user produces the counts (`[gaze] speech: yaw=… hold=…/… -> WOULD_WAKE`) that settle what angle reads as "addressing the lamp".

Degradation is by omission in both directions. On a device with **no camera** the watcher never arms and the other two openers are untouched — no separate configuration. When `HAL_WAKEWORD_ENABLED` is **false** the watcher does not start at all: with no wake word every utterance already dispatches, so there is no gate left to open and the check would burn CPU to decide nothing. A gaze sample is also skipped while the head is relocating, when the camera is disabled for privacy, and whenever the detector lock is held by a live `look`.

**Relocating, not merely writing.** Two states write the servos continuously without moving the head anywhere: the idle loop breathing, and a tracking session pursuing the user's face. Treating either as a move means `last_servo_write` is never stale and nearly every frame is refused — measured, idle: 0.3 samples/s recorded against 4.9/s blocked; measured, tracking: 0.7/s against 4.5/s, refusing a user at yaw 0.9° with a 130 px face dead centre for having one sample in the window instead of two. Tracking matters most: it is the lamp following this user's face, so refusing to notice they are addressing it precisely then is the most broken-looking moment available — which is why the settling test must not become `_tracking_active` by the back door. Both are small continuous corrections and the yaw survives them. The `[gaze] sampling at N/s; blocked: …` line breaks the blocked count down by reason, because the two gates are fixed in different places.

End-to-end chain:
1. `gpio_button.py` / `ttp223.py` detect single click → call `single_click_action(source)` in `button_actions.py`
2. `single_click_action` → `_cancel_agent_speech()` (fire-and-forget thread) + active `tracker_service.stop()` + `stop_tts()` (routes/voice.py) + `audio_stop()` (routes/music.py) + deferred `_announce_listening()` thread
2a. `_cancel_agent_speech()` → `POST /api/agent/speech/cancel` on the OS server. Needed because `stop_tts()` only silences what HAL already holds: the sentence playing plus the pre-synthesised queue. The OS server streams a reply sentence by sentence, so without this call the device goes quiet for one sentence and then talks on. The OS server mutes every turn in flight (see `docs/os-server.md`) while letting turns started after the click speak — so the user can tap and immediately say something new even with a backlog of older turns still draining. The turns are not aborted, only unspoken. Dispatched on its own thread and fired on both branches (mic-unmute and stop-speaker), since either way the tap means the user is taking the floor.
2b. `state.note_music_cancel()` → stamps a HAL-side music cancel watermark, and `audio_stop()` runs on **both** branches (mic-unmute and stop-speaker), not just the stop-speaker one. Needed because the OS server's cancel is TTS-only: the cancelled turn keeps running and its pending music tool call still reaches `POST /audio/play` a moment later, where a fresh `music-play` thread clears its own `_stop_event` — so a point-in-time stop always loses that race and the user hears music they just cancelled once `yt-dlp` finishes resolving (1–5 s). While the watermark is fresh (`app_state.MUSIC_CANCEL_GUARD_S`, 3 s) `/audio/play` answers `{"status": "suppressed"}` instead of playing. The window is sized to cover the in-flight tool call but stay under the floor of a genuinely new request (speak → STT → LLM → tool is never under ~3 s), so "tap, then ask for a song" still works.
3. `stop_tts()` → `tts_service.stop()` sets `_stop_event`; every blocking loop in TTS streaming (synth, render, playback) honors the event and aborts cleanly without leaving the speaker pegged

### Voice barge-in (optional, off by default)

Voice-driven interrupt — speak during TTS to make Lamp stop and listen — is gated behind `HAL_BARGE_IN_ENABLED=true` in `hal/.env`. When enabled, `voice_service._monitor_barge_in()` opens a parallel mic capture during TTS playback, computes RMS over 256ms blocks, and calls `tts_service.stop()` when N consecutive blocks exceed `HAL_BARGE_IN_RMS_THRESHOLD`. Same downstream chain as tap-to-interrupt.

Why off by default: software-only AEC is not viable on this hardware (Speex AEC integration degrades to ~13-30% reduction under multi-chunk TTS streaming). With only physical mic-speaker separation, bleed RMS (1-7500 observed) and user voice RMS (6-14k observed) overlap in the 7-9k zone, so a single RMS threshold cannot discriminate cleanly. Threshold 9000 + 1-frame trigger biases toward zero false-trigger at the cost of needing loud, deliberate utterance to trigger; threshold 6000-7000 biases the other way. Tuning per deployment is unavoidable until the device gains hardware AEC (e.g. ReSpeaker XVF3800).

When enabled, tail the log for `Barge-in monitor session end: max_rms_seen=N` (peak per session) and `BARGE-IN: RMS=N` events to characterize the deployed mic, then set `HAL_BARGE_IN_RMS_THRESHOLD` midway between observed bleed-max and voice-min. Tap-to-interrupt remains active regardless.

## GPIO button detection (`hal/drivers/gpio_button.py`)

Edge-counting driver where **all destructive actions commit on the release edge based on hold duration** — no timer fires while the button is held. This is what lets the user cancel mid-hold (release before a threshold) or escalate (keep holding past 10 s).

1. **Falling edge (press):** record `press_start` (monotonic clock) and spawn a hold-LED watcher thread (one per press, with its own stop `Event`). No action timer is armed.
2. **Rising edge (release):** stop the LED watcher, then compute `held = now − press_start` and branch:
   - `held >= 10 s` (`FACTORY_RESET_DURATION`) → scrub any pending clicks, lock LED solid red, run `factory_reset_action` off-thread.
   - `held >= 5 s` (`LONG_PRESS_DURATION`) → scrub pending clicks, freeze LED red, run `long_press_action` (shutdown) off-thread.
   - `held >= 2 s` (`SLEEP_HOLD_DURATION`) → scrub any pending clicks, run `sleep_action` off-thread; it invokes the standard `sleepy` emotion pipeline.
   - else (short tap) → increment `click_count` and (re)start a 0.4 s click-window timer. On the **first** tap of a burst, the silent part of `single_click_action` (`announce=False`) fires immediately off-thread — it's non-destructive ("give me the floor"), so it doesn't wait for the window. The audible cue is deferred so it never talks over a triple-click in progress.
3. When the click window expires:
   - `count == 3` → `triple_click_action` (no listening cue — only the reboot announce)
   - any other count → `announce_listening_cue` speaks the deferred "Listening" confirmation once per burst; `count == 2` / `>= 4` additionally log as ignored (panic-click guard — the floor-grab already happened on tap 1, nothing destructive fires)

A release edge with no matching press (the press was debounce-dropped) is ignored — `press_start` could be stale, so acting on it could fire a destructive action against a minutes-old timestamp. Destructive actions run on their own daemon threads because the `lgpio` callback must return promptly or subsequent edges queue up.

### Hold LED feedback

The watcher thread polls the hold duration and drives the RGB LED at HIGH priority (preempts the current emotion) so the user sees how far they've armed before they release:

| Hold elapsed | LED | Meaning |
|---|---|---|
| < 2 s | unchanged | a short tap |
| 2–5 s | sleepy purple, blinking 2 Hz | sleepy is armed; releasing enters sleep (LED then turns off) |
| 5–10 s | red, blinking 2 Hz | shutdown armed — releasing now shuts down |
| 10 s+ | red, solid | factory-reset armed — releasing now wipes + reboots |

Purple identifies the sleep tier; red blink vs red solid differentiates shutdown from factory-reset. The LED is a silent no-op when the RGB service is unavailable (dev machines) — the button still works.

Per-edge debounce is 200 ms (press and release ticks tracked independently so a quick tap isn't dropped while bouncy repeats of the same edge are filtered).

## TTP223 detection (`hal/drivers/ttp223.py`)

The TTP223 IC on this board runs in **FastMode**: output goes HIGH on touch, then automatically drops back LOW within ~50-80 ms even with the finger still on the pad. The IC re-triggers only when capacitance changes meaningfully (finger moves). Continuous "hold" is impossible without rewiring the IC's FM pin to LowPowerMode (~12 s max touch).

Cross-talk between adjacent pads is also significant — a single physical touch fires edges on 2-4 pads with staggered timing.

The driver compensates with a **two-layer model**:

### Layer 1: Session (200 ms gap)

Any edge — rising or falling, any pad — restarts a 200 ms timer. When the timer expires (no new edges for 200 ms), the "session" ends. One session = one logical touch event from the user's perspective, regardless of how many physical edges fired inside it (cross-talk + FastMode auto-LOW pulses).

### Layer 2: Decision window (1.2 s after session end)

After a session ends:

1. If a **pet cooldown** is active (a head-pat fired recently), the session is silently absorbed and the cooldown is extended. Prevents stuttering `single_click` interjections between continuous strokes.
2. Otherwise increment the session count. On the **first** session of a burst (`_ack_first_session`): if TTS is mid-utterance, speech is stopped immediately, then a short ack chime plays (gesture-neutral — valid for a tap or the first stroke of a pet). TTS stop + chime only — music, unmute and the listening cue still wait for resolution. Deliberate trade-off: petting Lamp while she talks now cuts her off (the pet giggle follows) in exchange for instant tap-to-interrupt.
3. Then resolve:
   - `count >= 2` → fire `head_pat_action` immediately, arm 1.5 s pet cooldown
   - `count < 2` → schedule a 1.2 s decision timer. When that timer fires with `count == 1`, fire `single_click_action`.

### Constants (`ttp223.py`)

| Constant | Value | Why |
|---|---|---|
| `SESSION_GAP_S` | 0.2 | Comfortably exceeds observed cross-talk burst (~30-100 ms) without merging genuinely separate taps |
| `DECISION_WINDOW_S` | 1.2 | Field-measured user stroke pace is 0.8-1.2 s per beat — wide enough to keep the first stroke of a pet motion from firing a spurious single_click |
| `PET_SESSION_THRESHOLD` | 2 | Two consecutive sessions within the decision window = pet. Easier than 3 because each "stroke" produces only one session on this hardware |
| `PET_COOLDOWN_S` | 1.5 | After a pet fires, additional sessions within 1.5 s extend the cooldown rather than starting a new count. Stroking continuously = one pet, then silence |

## Shared action library (`hal/drivers/button_actions.py`)

The actions live in one place so the GPIO button, TTP223, and any future input (touchpad, remote) get identical behavior:

| Function | What it does | Interrupts in-flight TTS? |
|---|---|---|
| `single_click_action(source)` | Stop active object tracking. Then relax a user/scene speaker mute (skipped while `_enrolling`). If mic is muted: unmute. Else stop TTS + stop music. Then open the wake-word follow-up window (no-op when wake word is off) and speak the localized "Listening" cue with retry-on-busy. Tracking still stops when the hardware mic kill switch is on; the voice action remains suppressed. | Yes — calls `stop_tts()` and the cue itself preempts. |
| `triple_click_action(source)` | Speak "Rebooting now" → wait 5 s for the cached clip → `sudo reboot`. | Yes |
| `sleep_action(source)` | Speak the localized sleep announcement, then invoke `sleepy`: LED off, camera/mic/speaker off, then servo release after 1 s. | Yes — the sleepy pipeline stops active TTS/music after the announcement. |
| `long_press_action(source)` | Speak "Shutting down now" → wait 5 s → `release_servos()` (so the lamp doesn't slam down mid-pose) → `sudo shutdown -h now`. | Yes |
| `factory_reset_action(source)` | Speak "Factory reset starting. Rebooting now" → `release_servos()` → POST `/api/system/factory-reset` on the OS server (the server owns the wipe + reboot, see below). | Yes |
| `head_pat_action(source)` | Pick a random localized pet phrase, speak it via `speak_cached` on a daemon thread. **Non-interrupting**: if TTS is still busy the phrase is dropped silently. In practice on TTP223 the first touch session already cut any in-flight speech (`_grab_floor_if_speaking`), so by pet time TTS is usually free and the giggle plays. | No |

### Factory-reset: what gets wiped

`factory_reset_action` only **announces + delegates** — the actual reset lives in the OS server (`system/server/system/factoryreset.go`), reachable from the device over loopback without a Bearer token (authoritative because of physical presence: a deliberate 10 s hold). `POST /api/system/factory-reset` is a **soft** reset (state wipe, not a reflash — kernel / OS packages / binaries / HAL `.venv` are untouched):

1. Wipe the active agent backend's state (OpenClaw or Hermes, auto-detected from `config.json` `agent_runtime`).
2. Wipe the device state paths: `/root/config` (config.json — API keys, channel tokens, MQTT creds), `/root/local/users` + `/root/local/strangers` (face/voice enrollments), `/var/lib/hal/snapshots` (camera snapshots), and `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` (home WiFi creds → forces AP mode on next boot).
3. Reboot. The device comes back up in AP mode `<device_type>-XXXX` with a fresh setup wizard (~30 s).

The reset is **single-flight** with a 5-minute cooldown (`FactoryResetMinInterval`) shared across all trigger surfaces (GPIO hold, HTTP, MQTT) — a circuit breaker against runaway callers and accidental repeats.

## Mute/disable persistence across HAL restarts

Mic mute, speaker mute, and camera disable each persist to their own boot-scoped
sidecar — `/tmp/hal-mic-state.json`, `/tmp/hal-speaker-state.json`,
`/tmp/hal-camera-state.json` (same `boot_id` pattern as the LED/scene sidecars) —
so a HAL service restart (OTA, deploy, config change) no longer silently unmutes
the mic, re-enables the speaker, or turns the camera back on. Every route that
flips a switch persists it (`/voice/mute|unmute`, `/speaker/mute|unmute`,
`/camera/disable|enable`, scene mic/speaker changes, `_auto_camera_on/off`); the
button/touchpad gestures go through the same routes. On restore: `start_voice`
builds the voice pipeline but doesn't open the mic, `server.py` lifespan skips
starting the camera capture and re-paints the mic-muted LED indicator, and the
speaker flag needs no apply step (TTS checks it at speak time). A full device
reboot starts fresh (on Intern v2 Pro the physical mic switch re-applies itself
anyway). Record-enroll's transient speaker mute is deliberately NOT persisted.

## Localized phrases

The action announcements are localized per `stt_language` from Lamp's `config.json`. Language constants live in `hal/presets.py` (`LANG_EN`, `LANG_VI`, `LANG_ZH_CN`, `LANG_ZH_TW`, `DEFAULT_LANG`). Falls back to `DEFAULT_LANG` (English) when the active language has no translation.

### Safety announcements (one phrase per language)

`reboot`, `shutdown`, `factory-reset`, and the `listening` cue use literal-meaning phrases ("Rebooting now", "Shutting down now", "Factory reset starting. Rebooting now") in every language because the user just performed a destructive gesture and needs unambiguous confirmation — this is a safety announcement, not a persona moment.

### Pet responses (15 phrases per language, random pick)

Pet phrases are picked at random from a 15-entry pool per language so Lamp doesn't sound robotic when petted repeatedly. Tone reflects Lamp's character (AI companion + smart light + expressive robot, "like a pet/friend"):

- Tickle / giggle: "Hehe, that tickles!" / "Hihi, nhột quá!"
- Pet-like purring: "I'm purring." / "Mình kêu rừ rừ nè!" / "我咕噜咕噜啦！"
- Light-themed (Lamp = luminous): "You light me up." / "Mình sáng cả lên rồi nè!"
- Warm heart: "My heart's glowing." / "Tim mình ấm lên!"
- Ask for more: "More, please!" / "Vuốt nữa đi mà!"
- Compliment giver: "You're the best." / "Mình mê cái này lắm!"
- Playful nũng: "Stop it, you!" / "Vuốt nhẹ thôi nha~"

Phrases are intentionally short — they fire mid-stroke and need to feel responsive.

## Files

| Path | Purpose |
|---|---|
| `hal/drivers/gpio_button.py` | GPIO button handler (mechanical, both boards) |
| `hal/drivers/ttp223.py` | TTP223 capacitive touchpad handler (OrangePi sun60 only) |
| `hal/drivers/button_actions.py` | Shared action functions + localized phrase pools |
| `hal/presets.py` | Language code constants (`LANG_EN`, etc.) |
| `hal/test_ttp223_probe_orangepi.py` | Standalone probe for verifying TTP223 line mapping |
| `hal/test_gpio.py` | Standalone probe for verifying GPIO button line |

Both handlers are spawned in `hal/server.py` lifespan startup — failures are logged but never crash the runtime (a board without the hardware just skips silently).
