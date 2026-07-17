# Physical Controls — GPIO Button + TTP223 Touchpad

Lamp has two physical input devices the user can touch directly. They share the same action library (`os/hal/drivers/button_actions.py`) so any gesture mapped to "single click" behaves identically whether it came from the mechanical button or the capacitive touchpad.

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
| **1 tap** | Stop speaker / unmute mic + speaker + ack chime (~120 ms ping) — all fire immediately on release (no click-window wait); the "I'm listening" cue plays once the 0.4 s click window resolves | Same, split the same way — in-flight TTS is cut and the ack chime plays ~0.2 s after the finger lifts (first session end); unmute + cue wait for the 1.2 s decision window (tap-vs-pet cost, see below) |
| **2 taps** (≤ 0.4 s apart, button) / (≤ 1.2 s apart, TTP223) | Nothing beyond the single-click already fired on tap 1 (panic-click guard) | Pet response — TTS picks a random phrase from the language pool |
| **3 taps** (≤ 0.4 s apart, button) | Reboot OS (TTS announce → `sudo reboot`) | n/a — TTP223 stops at 2 (any further taps absorbed by cooldown) |
| **Hold 5–10 s, then release** | Shutdown OS (TTS announce → release servos → `sudo shutdown -h now`). LED blinks red while armed. | n/a — TTP223 hardware cannot reliably hold (see "FastMode" below) |
| **Hold 10 s+, then release** | Factory-reset: wipe device state + reboot into AP setup (TTS announce → release servos → POST `/api/system/factory-reset` on the OS server). LED goes solid red while armed. | n/a |

Destructive gestures (reboot, shutdown, factory-reset) are intentionally only on the GPIO button. Hard actions need a deliberate gesture, and the mechanical button gives unambiguous evidence of intent. The two hold tiers **commit on release, not on a timer firing while held** — so the user can cancel by releasing before crossing a threshold, or keep holding past 10 s to escalate from shutdown to factory-reset (see "GPIO button detection" below).

## Interrupting Lamp while it speaks (barge-in)

The 1-tap gesture is Lamp's primary **barge-in mechanism**: tap top of Lamp (touchpad) or press the GPIO button once during an in-flight TTS to cancel the current utterance mid-word, stop any music, and unmute the mic so Lamp listens for the next thing the user says. A user/scene speaker mute is also relaxed (unless a voice enrollment is recording) so the cue and the reply are audible again. A localized "I'm listening" cue plays after the cancel.

End-to-end chain:
1. `gpio_button.py` / `ttp223.py` detect single click → call `single_click_action(source)` in `button_actions.py`
2. `single_click_action` → `stop_tts()` (routes/voice.py) + `audio_stop()` (routes/music.py) + deferred `_announce_listening()` thread
3. `stop_tts()` → `tts_service.stop()` sets `_stop_event`; every blocking loop in TTS streaming (synth, render, playback) honors the event and aborts cleanly without leaving the speaker pegged

### Voice barge-in (optional, off by default)

Voice-driven interrupt — speak during TTS to make Lamp stop and listen — is gated behind `HAL_BARGE_IN_ENABLED=true` in `os/hal/.env`. When enabled, `voice_service._monitor_barge_in()` opens a parallel mic capture during TTS playback, computes RMS over 256ms blocks, and calls `tts_service.stop()` when N consecutive blocks exceed `HAL_BARGE_IN_RMS_THRESHOLD`. Same downstream chain as tap-to-interrupt.

Why off by default: software-only AEC is not viable on this hardware (Speex AEC integration degrades to ~13-30% reduction under multi-chunk TTS streaming). With only physical mic-speaker separation, bleed RMS (1-7500 observed) and user voice RMS (6-14k observed) overlap in the 7-9k zone, so a single RMS threshold cannot discriminate cleanly. Threshold 9000 + 1-frame trigger biases toward zero false-trigger at the cost of needing loud, deliberate utterance to trigger; threshold 6000-7000 biases the other way. Tuning per deployment is unavoidable until the device gains hardware AEC (e.g. ReSpeaker XVF3800).

When enabled, tail the log for `Barge-in monitor session end: max_rms_seen=N` (peak per session) and `BARGE-IN: RMS=N` events to characterize the deployed mic, then set `HAL_BARGE_IN_RMS_THRESHOLD` midway between observed bleed-max and voice-min. Tap-to-interrupt remains active regardless.

## GPIO button detection (`os/hal/drivers/gpio_button.py`)

Edge-counting driver where **all destructive actions commit on the release edge based on hold duration** — no timer fires while the button is held. This is what lets the user cancel mid-hold (release before a threshold) or escalate (keep holding past 10 s).

1. **Falling edge (press):** record `press_start` (monotonic clock) and spawn a hold-LED watcher thread (one per press, with its own stop `Event`). No action timer is armed.
2. **Rising edge (release):** stop the LED watcher, then compute `held = now − press_start` and branch:
   - `held >= 10 s` (`FACTORY_RESET_DURATION`) → scrub any pending clicks, lock LED solid red, run `factory_reset_action` off-thread.
   - `held >= 5 s` (`LONG_PRESS_DURATION`) → scrub pending clicks, freeze LED red, run `long_press_action` (shutdown) off-thread.
   - else (short tap) → increment `click_count` and (re)start a 0.4 s click-window timer. On the **first** tap of a burst, the silent part of `single_click_action` (`announce=False`) fires immediately off-thread — it's non-destructive ("give me the floor"), so it doesn't wait for the window. The audible cue is deferred so it never talks over a triple-click in progress.
3. When the click window expires:
   - `count == 3` → `triple_click_action` (no listening cue — only the reboot announce)
   - any other count → `announce_listening_cue` speaks the deferred "I'm listening" confirmation once per burst; `count == 2` / `>= 4` additionally log as ignored (panic-click guard — the floor-grab already happened on tap 1, nothing destructive fires)

A release edge with no matching press (the press was debounce-dropped) is ignored — `press_start` could be stale, so acting on it could fire a destructive action against a minutes-old timestamp. Destructive actions run on their own daemon threads because the `lgpio` callback must return promptly or subsequent edges queue up.

### Hold LED feedback

The watcher thread polls the hold duration and drives the RGB LED at HIGH priority (preempts the current emotion) so the user sees how far they've armed before they release:

| Hold elapsed | LED | Meaning |
|---|---|---|
| < 5 s | unchanged | below shutdown threshold — releasing is a tap |
| 5–10 s | red, blinking 1 Hz | shutdown armed — releasing now shuts down |
| 10 s+ | red, solid | factory-reset armed — releasing now wipes + reboots |

Same red colour for both armed tiers; blink vs solid is the differentiator. The LED is a silent no-op when the RGB service is unavailable (dev machines) — the button still works.

Per-edge debounce is 200 ms (press and release ticks tracked independently so a quick tap isn't dropped while bouncy repeats of the same edge are filtered).

## TTP223 detection (`os/hal/drivers/ttp223.py`)

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

## Shared action library (`os/hal/drivers/button_actions.py`)

The actions live in one place so the GPIO button, TTP223, and any future input (touchpad, remote) get identical behavior:

| Function | What it does | Interrupts in-flight TTS? |
|---|---|---|
| `single_click_action(source)` | Relax a user/scene speaker mute (skipped while `_enrolling`). If mic is muted: unmute. Else stop TTS + stop music. Then speak the localized "I'm listening" cue with retry-on-busy. | Yes — calls `stop_tts()` and the cue itself preempts. |
| `triple_click_action(source)` | Speak "Rebooting now" → wait 5 s for the cached clip → `sudo reboot`. | Yes |
| `long_press_action(source)` | Speak "Shutting down now" → wait 5 s → `release_servos()` (so the lamp doesn't slam down mid-pose) → `sudo shutdown -h now`. | Yes |
| `factory_reset_action(source)` | Speak "Factory reset starting. Rebooting now" → `release_servos()` → POST `/api/system/factory-reset` on the OS server (the server owns the wipe + reboot, see below). | Yes |
| `head_pat_action(source)` | Pick a random localized pet phrase, speak it via `speak_cached` on a daemon thread. **Non-interrupting**: if TTS is still busy the phrase is dropped silently. In practice on TTP223 the first touch session already cut any in-flight speech (`_grab_floor_if_speaking`), so by pet time TTS is usually free and the giggle plays. | No |

### Factory-reset: what gets wiped

`factory_reset_action` only **announces + delegates** — the actual reset lives in the OS server (`os/services/server/system/factoryreset.go`), reachable from the device over loopback without a Bearer token (authoritative because of physical presence: a deliberate 10 s hold). `POST /api/system/factory-reset` is a **soft** reset (state wipe, not a reflash — kernel / OS packages / binaries / HAL `.venv` are untouched):

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

The action announcements are localized per `stt_language` from Lamp's `config.json`. Language constants live in `os/hal/presets.py` (`LANG_EN`, `LANG_VI`, `LANG_ZH_CN`, `LANG_ZH_TW`, `DEFAULT_LANG`). Falls back to `DEFAULT_LANG` (English) when the active language has no translation.

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
| `os/hal/drivers/gpio_button.py` | GPIO button handler (mechanical, both boards) |
| `os/hal/drivers/ttp223.py` | TTP223 capacitive touchpad handler (OrangePi sun60 only) |
| `os/hal/drivers/button_actions.py` | Shared action functions + localized phrase pools |
| `os/hal/presets.py` | Language code constants (`LANG_EN`, etc.) |
| `os/hal/test_ttp223_probe_orangepi.py` | Standalone probe for verifying TTP223 line mapping |
| `os/hal/test_gpio.py` | Standalone probe for verifying GPIO button line |

Both handlers are spawned in `os/hal/server.py` lifespan startup — failures are logged but never crash the runtime (a board without the hardware just skips silently).
