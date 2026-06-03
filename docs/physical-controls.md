# Physical Controls — GPIO Button + TTP223 Touchpad

Lamp has two physical input devices the user can touch directly. They share the same action library (`lelamp/service/button_actions.py`) so any gesture mapped to "single click" behaves identically whether it came from the mechanical button or the capacitive touchpad.

## Why two devices

| Device | Role | Where |
|---|---|---|
| **GPIO button** | One mechanical button. Used for decisive actions including destructive ones (reboot/shutdown). The mechanical feel and long-hold detection make accidental destructive actions unlikely. | Both Pi 4/5 and OrangePi sun60 |
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
| **1 tap** | Stop speaker / unmute mic + announce "I'm listening" | Same — fires ~1.2 s after release (decision-window cost, see below) |
| **2 taps** (≤ 0.4 s apart, button) / (≤ 1.2 s apart, TTP223) | Ignored (panic-click guard) | Pet response — TTS picks a random phrase from the language pool |
| **3 taps** (≤ 0.4 s apart, button) | Reboot OS (TTS announce → `sudo reboot`) | n/a — TTP223 stops at 2 (any further taps absorbed by cooldown) |
| **Hold 5 s** | Shutdown OS (TTS announce → release servos → `sudo shutdown -h now`) | n/a — TTP223 hardware cannot reliably hold (see "FastMode" below) |

Destructive gestures (reboot, shutdown) are intentionally only on the GPIO button. Hard actions need a deliberate gesture, and the mechanical button gives unambiguous evidence of intent.

## Interrupting Lamp while it speaks (barge-in)

The 1-tap gesture is Lamp's primary **barge-in mechanism**: tap top of Lamp (touchpad) or press the GPIO button once during an in-flight TTS to cancel the current utterance mid-word, stop any music, and unmute the mic so Lamp listens for the next thing the user says. A localized "I'm listening" cue plays after the cancel.

End-to-end chain:
1. `gpio_button.py` / `ttp223.py` detect single click → call `single_click_action(source)` in `button_actions.py`
2. `single_click_action` → `stop_tts()` (routes/voice.py) + `audio_stop()` (routes/music.py) + deferred `_announce_listening()` thread
3. `stop_tts()` → `tts_service.stop()` sets `_stop_event`; every blocking loop in TTS streaming (synth, render, playback) honors the event and aborts cleanly without leaving the speaker pegged

### Voice barge-in (optional, off by default)

Voice-driven interrupt — speak during TTS to make Lamp stop and listen — is gated behind `LELAMP_BARGE_IN_ENABLED=true` in `lelamp/.env`. When enabled, `voice_service._monitor_barge_in()` opens a parallel mic capture during TTS playback, computes RMS over 256ms blocks, and calls `tts_service.stop()` when N consecutive blocks exceed `LELAMP_BARGE_IN_RMS_THRESHOLD`. Same downstream chain as tap-to-interrupt.

Why off by default: software-only AEC is not viable on this hardware (Speex AEC integration degrades to ~13-30% reduction under multi-chunk TTS streaming). With only physical mic-speaker separation, bleed RMS (1-7500 observed) and user voice RMS (6-14k observed) overlap in the 7-9k zone, so a single RMS threshold cannot discriminate cleanly. Threshold 9000 + 1-frame trigger biases toward zero false-trigger at the cost of needing loud, deliberate utterance to trigger; threshold 6000-7000 biases the other way. Tuning per deployment is unavoidable until the device gains hardware AEC (e.g. ReSpeaker XVF3800).

When enabled, tail the log for `Barge-in monitor session end: max_rms_seen=N` (peak per session) and `BARGE-IN: RMS=N` events to characterize the deployed mic, then set `LELAMP_BARGE_IN_RMS_THRESHOLD` midway between observed bleed-max and voice-min. Tap-to-interrupt remains active regardless.

## GPIO button detection (`lelamp/service/gpio_button.py`)

Standard edge-counting button driver, mirrors typical patterns:

1. Each falling edge (press) starts a 5 s long-press timer.
2. Each rising edge (release) cancels the timer. If the press was shorter than 5 s, increment `click_count` and (re)start a 0.4 s click-window timer.
3. When the click window expires:
   - `count == 1` → `single_click_action`
   - `count == 3` → `triple_click_action`
   - `count == 2` or `>= 4` → ignored (panic-click guard)
4. If the 5 s long-press timer fires:
   - Re-read the pin level (defensive — guards against missed release edges that would otherwise trigger shutdown on a slow double-tap)
   - If pin is still LOW (button held), fire `long_press_action`
   - Otherwise log and bail

Per-edge debounce is 200 ms.

## TTP223 detection (`lelamp/service/ttp223.py`)

The TTP223 IC on this board runs in **FastMode**: output goes HIGH on touch, then automatically drops back LOW within ~50-80 ms even with the finger still on the pad. The IC re-triggers only when capacitance changes meaningfully (finger moves). Continuous "hold" is impossible without rewiring the IC's FM pin to LowPowerMode (~12 s max touch).

Cross-talk between adjacent pads is also significant — a single physical touch fires edges on 2-4 pads with staggered timing.

The driver compensates with a **two-layer model**:

### Layer 1: Session (200 ms gap)

Any edge — rising or falling, any pad — restarts a 200 ms timer. When the timer expires (no new edges for 200 ms), the "session" ends. One session = one logical touch event from the user's perspective, regardless of how many physical edges fired inside it (cross-talk + FastMode auto-LOW pulses).

### Layer 2: Decision window (1.2 s after session end)

After a session ends:

1. If a **pet cooldown** is active (a head-pat fired recently), the session is silently absorbed and the cooldown is extended. Prevents stuttering `single_click` interjections between continuous strokes.
2. Otherwise increment the session count and:
   - `count >= 2` → fire `head_pat_action` immediately, arm 1.5 s pet cooldown
   - `count < 2` → schedule a 1.2 s decision timer. When that timer fires with `count == 1`, fire `single_click_action`.

### Constants (`ttp223.py`)

| Constant | Value | Why |
|---|---|---|
| `SESSION_GAP_S` | 0.2 | Comfortably exceeds observed cross-talk burst (~30-100 ms) without merging genuinely separate taps |
| `DECISION_WINDOW_S` | 1.2 | Field-measured user stroke pace is 0.8-1.2 s per beat — wide enough to keep the first stroke of a pet motion from firing a spurious single_click |
| `PET_SESSION_THRESHOLD` | 2 | Two consecutive sessions within the decision window = pet. Easier than 3 because each "stroke" produces only one session on this hardware |
| `PET_COOLDOWN_S` | 1.5 | After a pet fires, additional sessions within 1.5 s extend the cooldown rather than starting a new count. Stroking continuously = one pet, then silence |

## Shared action library (`lelamp/service/button_actions.py`)

All three actions live in one place so the GPIO button, TTP223, and any future input (touchpad, remote) get identical behavior:

| Function | What it does | Interrupts in-flight TTS? |
|---|---|---|
| `single_click_action(source)` | If mic is muted: unmute. Else stop TTS + stop music. Then speak the localized "I'm listening" cue with retry-on-busy. | Yes — calls `stop_tts()` and the cue itself preempts. |
| `triple_click_action(source)` | Speak "Rebooting now" → wait 5 s for the cached clip → `sudo reboot`. | Yes |
| `long_press_action(source)` | Speak "Shutting down now" → wait 5 s → `release_servos()` (so the lamp doesn't slam down mid-pose) → `sudo shutdown -h now`. | Yes |
| `head_pat_action(source)` | Pick a random localized pet phrase, speak it via `speak_cached` on a daemon thread. **Non-interrupting**: if TTS is already speaking, the phrase is dropped silently — petting mid-sentence shouldn't truncate Lamp. | No |

## Localized phrases

All four actions are localized per `stt_language` from Lamp's `config.json`. Language constants live in `lelamp/presets.py` (`LANG_EN`, `LANG_VI`, `LANG_ZH_CN`, `LANG_ZH_TW`, `DEFAULT_LANG`). Falls back to `DEFAULT_LANG` (English) when the active language has no translation.

### Safety announcements (one phrase per language)

`reboot`, `shutdown`, and the `listening` cue use literal-meaning phrases ("Rebooting now", "Shutting down now") in every language because the user just performed a destructive gesture and needs unambiguous confirmation — this is a safety announcement, not a persona moment.

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
| `lelamp/service/gpio_button.py` | GPIO button handler (mechanical, both boards) |
| `lelamp/service/ttp223.py` | TTP223 capacitive touchpad handler (OrangePi sun60 only) |
| `lelamp/service/button_actions.py` | Shared action functions + localized phrase pools |
| `lelamp/presets.py` | Language code constants (`LANG_EN`, etc.) |
| `lelamp/test_ttp223_probe_orangepi.py` | Standalone probe for verifying TTP223 line mapping |
| `lelamp/test_gpio.py` | Standalone probe for verifying GPIO button line |

Both handlers are spawned in `lelamp/server.py` lifespan startup — failures are logged but never crash the runtime (a board without the hardware just skips silently).
