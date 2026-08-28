"""
HAL presets — emotion, scene, and LED effect constants.

All pure data, no runtime dependencies. Import from server.py.
"""

# --- Language code constants (device stt_language / TTS language) ---
# Keep these aligned with the language codes returned by /os-server config and
# the keys used in TTS phrase dictionaries. DEFAULT_LANG is the fallback
# when stt_language is empty or unknown.
LANG_EN = "en"
LANG_VI = "vi"
LANG_ZH_CN = "zh-CN"
LANG_ZH_TW = "zh-TW"
SUPPORTED_LANGS = [LANG_EN, LANG_VI, LANG_ZH_CN, LANG_ZH_TW]
DEFAULT_LANG = LANG_EN

# --- LED state types (tracked in _user_led_state["type"]) ---
LST_SOLID = "solid"
LST_PAINT = "paint"
LST_EFFECT = "effect"
LST_SCENE = "scene"
LST_OFF = "off"

# --- RGB dispatch commands (rgb_service.dispatch(cmd, ...)) ---
RGB_CMD_SOLID = "solid"
RGB_CMD_PAINT = "paint"

# --- Servo dispatch commands (animation_service.dispatch(cmd, ...)) ---
SERVO_CMD_PLAY = "play"
SERVO_CMD_MUSIC_START = "music_start"
SERVO_CMD_MUSIC_STOP = "music_stop"

# --- LED effect name constants ---
FX_BREATHING = "breathing"
# Breathing whose fractional level is spread across the ring instead of being
# truncated per pixel — see effects.breathing_fine. For cues that must stay
# readable at a peak of a few units, where plain breathing has 2 usable levels.
FX_BREATHING_FINE = "breathing_fine"
FX_CANDLE = "candle"
FX_RAINBOW = "rainbow"
FX_NOTIFICATION_FLASH = "notification_flash"
FX_PULSE = "pulse"
FX_BLINK = "blink"
FX_SPEAKING_WAVE = "speaking_wave"
FX_SPEAKING_WAVE_RAINBOW = "speaking_wave_rainbow"

VALID_LED_EFFECTS = [FX_BREATHING, FX_BREATHING_FINE, FX_CANDLE, FX_RAINBOW, FX_NOTIFICATION_FLASH, FX_PULSE, FX_BLINK, FX_SPEAKING_WAVE,
                     FX_SPEAKING_WAVE_RAINBOW]

# --- Scene name constants ---
SCENE_READING = "reading"
SCENE_FOCUS = "focus"
SCENE_RELAX = "relax"
SCENE_MOVIE = "movie"
SCENE_NIGHT = "night"
SCENE_ENERGIZE = "energize"

# --- Aim direction constants ---
AIM_CENTER = "center"
AIM_DESK = "desk"
AIM_WALL = "wall"
AIM_LEFT = "left"
AIM_RIGHT = "right"
AIM_UP = "up"
AIM_DOWN = "down"
AIM_USER = "user"

# --- Servo recording name constants ---
# Each maps to a CSV file under recordings/ (e.g. SERVO_CURIOUS → "curious" → curious.csv).
SERVO_CURIOUS = "curious"
SERVO_HAPPY_WIGGLE = "happy_wiggle"
SERVO_SAD = "sad"
SERVO_THINKING_DEEP = "thinking_deep"
SERVO_IDLE = "idle"
SERVO_EXCITED = "excited"
SERVO_SHY = "shy"
SERVO_SHOCK = "shock"
SERVO_LISTENING = "listening"
SERVO_LAUGH = "laugh"
SERVO_CONFUSED = "confused"
SERVO_SLEEPY = "sleepy"
SERVO_GREETING = "greeting"
SERVO_GOODBYE = "goodbye"
SERVO_NOD = "nod"
SERVO_ACKNOWLEDGE = "acknowledge"
SERVO_STRETCHING = "stretching"
SERVO_SCANNING = "scanning"
SERVO_HEADSHAKE = "headshake"
SERVO_WAKE_UP = "wake_up"
SERVO_MUSIC_GROOVE = "music_groove"
SERVO_MUSIC_JAZZ = "music_jazz"
SERVO_MUSIC_CLASSICAL = "music_classical"
SERVO_MUSIC_HIPHOP = "music_hiphop"
SERVO_MUSIC_ROCK = "music_rock"
SERVO_MUSIC_WALTZ = "music_waltz"
SERVO_MUSIC_CHILL = "music_chill"
SERVO_MUSIC_HYPE = "music_hype"

# --- Emotion name constants ---
# Used as keys in EMOTION_PRESETS and for comparisons across the codebase.
# The string values are part of the HTTP API contract (SKILL.md).
EMO_CURIOUS = "curious"
EMO_HAPPY = "happy"
EMO_SAD = "sad"
EMO_THINKING = "thinking"
EMO_IDLE = "idle"
EMO_EXCITED = "excited"
EMO_SHY = "shy"
EMO_SHOCK = "shock"
EMO_LISTENING = "listening"
EMO_LAUGH = "laugh"
EMO_CONFUSED = "confused"
EMO_SLEEPY = "sleepy"
EMO_GREETING = "greeting"
EMO_GOODBYE = "goodbye"
EMO_CARING = "caring"
EMO_ACKNOWLEDGE = "acknowledge"
EMO_STRETCHING = "stretching"
EMO_MUSIC_STRONG = "music_strong"
EMO_MUSIC_CHILL = "music_chill"
EMO_SCAN = "scan"
EMO_NOD = "nod"
EMO_HEADSHAKE = "headshake"

# Emotion presets: maps emotion name to servo recording + LED color + optional LED effect.
# "effect" triggers a background LED animation; "color" is the base color for that effect.
# When no "effect" is set, LED is a simple solid fill.
# "camera": "off" = auto-disable camera (e.g. sleepy — device going to sleep)
# "camera": "on"  = auto-enable camera if off (active interaction, need vision)
# omitted         = no camera change
# Brightness: emotions are INDICATORS, not illumination. They follow the SAME
# peak budget as STATUS_LED_PRESETS — read the long note above that dict before
# changing a color here, it explains why the numbers look absurdly low:
#
#   green-dominant hue (green, yellow, cyan, white) -> peak channel 12
#   little or no green (red, purple, orange, blue)   -> peak channel 16
#
# The light.max_brightness gate (lamp: 120) does NOT dim these — it only scales
# a color's PEAK up to the ceiling — so dimming has to happen here.
#
# An earlier pass ran these at peak 25 (roughly 2x the status cues) on the
# theory that emotions are transient and can afford to be brighter. Tested by
# eye on a lamp (18/08/2026) that was wrong: curious and acknowledge were
# called glaring, and being an emotion rather than a status does not change how
# the strip meets the eye. Keep both dicts on one budget.
#
# Each color was dimmed by scaling its ORIGINAL channel ratios down to the tier
# above, so every emotion keeps the hue it has always had. Dim these by scaling,
# never by picking a new color — the hue is what the agent means, the level is
# only how loudly it says it.
#
# If you ever put a cue on FX_BLINK: blink() maps speed 1.0 to ~3 Hz
# (drivers/rgb/effects.py), fast enough to be actively unpleasant on a strip in
# the user's eyeline. Keep blink at 0.5 or below (~1.5 Hz and slower).
EMOTION_PRESETS = {
    EMO_CURIOUS: {"servo": SERVO_CURIOUS, "color": [12, 8, 0], "effect": FX_CANDLE, "speed": 0.3, "camera": "on"},
    EMO_HAPPY: {"servo": SERVO_HAPPY_WIGGLE, "color": [12, 9, 1], "effect": FX_CANDLE, "speed": 0.2, "camera": "on"},
    EMO_SAD: {"servo": SERVO_SAD, "color": [16, 8, 8], "effect": FX_BREATHING, "speed": 0.4, "camera": "on"},
    EMO_THINKING: {"servo": SERVO_THINKING_DEEP,
                   "color": [6, 12, 4],
                   "effect": FX_PULSE,
                   "speed": 0.3,
                   "camera": "on"},
    EMO_IDLE: {"servo": SERVO_IDLE, "color": [12, 8, 1], "effect": FX_BREATHING, "speed": 0.2},
    EMO_EXCITED: {"servo": SERVO_EXCITED, "color": [12, 8, 12], "effect": FX_CANDLE, "speed": 0.5, "camera": "on"},
    EMO_SHY: {"servo": SERVO_SHY, "color": [16, 7, 2], "effect": FX_BREATHING, "speed": 0.3, "camera": "on"},
    # White flash held at the same peak as STATUS_LED_PRESETS["ready_flash"]:
    # full-value white is the harshest thing the strip can do, and being a brief
    # flash does not soften it (tested by eye on a lamp). White is
    # green-dominant, so it takes the 12 tier of the peak budget documented at
    # STATUS_LED_PRESETS. Keep these two in step — same visual cue.
    EMO_SHOCK: {"servo": SERVO_SHOCK, "color": [12, 12, 12], "effect": FX_NOTIFICATION_FLASH, "speed": 1.0,
                "camera": "on"},
    # Breathing, not pulse: listening stays lit for as long as the user is
    # talking, and pulse's dark gap between beats reads as an alert on a cue
    # that long. A smooth breath says "open, waiting for you".
    EMO_LISTENING: {"servo": None,  # SERVO_LISTENING,
                    "color": [4, 8, 16],
                    "effect": FX_BREATHING_FINE,
                    "speed": 1.2,
                    # The one cue that must be readable the instant it fires:
                    # it answers the user's first words. The breath's rise is
                    # too slow to do that on a body that has slowed the speed
                    # down and dimmed the colour (lamp: [0,0,3] @ 0.3 renders
                    # black for the first ~1.1s), so this arc opens at full
                    # brightness and breathes down from there. Peak level and
                    # hue are untouched — nothing here gets brighter than it
                    # already was.
                    "start_at_peak": True,
                    "camera": "on"},
    EMO_LAUGH: {"servo": SERVO_LAUGH, "color": [12, 8, 1], "effect": FX_CANDLE, "speed": 0.2, "camera": "on"},
    EMO_CONFUSED: {"servo": SERVO_CONFUSED, "color": [16, 9, 3], "effect": FX_CANDLE, "speed": 0.2, "camera": "on"},
    EMO_SLEEPY: {"servo": SERVO_SLEEPY, "color": [0, 0, 0], "camera": "off", "mic": "off", "speaker": "off"},
    EMO_GREETING: {"servo": SERVO_GREETING, "color": [12, 8, 5], "effect": FX_BREATHING, "speed": 0.3, "camera": "on"},
    EMO_GOODBYE: {"servo": SERVO_GOODBYE, "color": [12, 8, 5], "effect": FX_BREATHING, "speed": 0.5},
    EMO_CARING: {"servo": SERVO_NOD, "color": [12, 8, 6], "effect": FX_BREATHING, "speed": 0.4, "camera": "on"},
    EMO_ACKNOWLEDGE: {"servo": SERVO_ACKNOWLEDGE, "color": [3, 12, 4], "effect": FX_BREATHING, "speed": 0.5,
                      "camera": "on"},
    EMO_STRETCHING: {"servo": SERVO_STRETCHING, "color": [12, 12, 2], "effect": FX_BREATHING, "speed": 0.6,
                     "camera": "on"},
    # music_strong is the one cue on FX_RAINBOW. rainbow sweeps the hue itself
    # and never reads "color" (the color is kept only because the emotion LED
    # path requires one to start an effect at all), so the peak budget above
    # cannot reach it and neither can the request's intensity. Its level is
    # "brightness" (0.0-1.0, same meaning as in SCENE_PRESETS) — the knob a
    # per-device presets.json overrides to dim the sweep. 1.0 here keeps the
    # base behavior; lamp turns it down.
    EMO_MUSIC_STRONG: {"servo": SERVO_MUSIC_ROCK, "color": [8, 12, 8], "effect": FX_RAINBOW, "speed": 1.0,
                       "brightness": 1.0},
    EMO_MUSIC_CHILL: {"servo": SERVO_MUSIC_ROCK, "color": [16, 9, 0], "effect": FX_BREATHING, "speed": 0.3},
    EMO_SCAN: {"servo": SERVO_SCANNING, "color": [5, 12, 3], "effect": FX_PULSE, "speed": 0.3, "camera": "on"},
    EMO_NOD: {"servo": SERVO_NOD, "color": [12, 8, 1], "effect": FX_BREATHING, "speed": 0.5, "camera": "on"},
    EMO_HEADSHAKE: {"servo": SERVO_HEADSHAKE, "color": [16, 6, 1], "effect": FX_BREATHING, "speed": 0.5,
                    "camera": "on"},
}

# Lighting scene presets — simulated color temperature via RGB mixing.
# 2200K = very warm amber, 2700K = warm white, 4000K = neutral, 5000K = cool, 6500K = daylight
# "camera": "off"/"on" = auto-disable/enable camera
# "mic": "off"/"on"    = mute/unmute microphone
# "speaker": "off"/"on"= mute/unmute speaker
# "servo": "hold"       = freeze servo (no idle/emotion animations)
# omitted               = no change for that peripheral
SCENE_PRESETS = {
    SCENE_READING: {"brightness": 0.80, "color": [255, 209, 163], "aim": AIM_DESK, "camera": "off", "mic": "on",
                    "speaker": "off", "servo": "hold"},  # ~4000K neutral; mic on for voice wake
    SCENE_FOCUS: {"brightness": 0.70, "color": [255, 214, 170], "aim": AIM_DESK, "camera": "off", "mic": "on",
                  "speaker": "off", "servo": "hold"},  # ~4200K warm-neutral; mic on for voice wake
    SCENE_RELAX: {"brightness": 0.40, "color": [255, 166, 87], "aim": AIM_WALL, "camera": "on", "mic": "on",
                  "speaker": "on"},  # ~2700K warm
    SCENE_MOVIE: {"brightness": 0.15, "color": [255, 147, 51], "aim": AIM_WALL, "camera": "off", "mic": "on",
                  "speaker": "off"},  # ~2400K dim amber
    SCENE_NIGHT: {"brightness": 0.05, "color": [255, 105, 0], "aim": AIM_DOWN, "camera": "off", "mic": "on",
                  "speaker": "off"},  # ~1800K deep amber, blue-free; mic stays on for voice wake
    SCENE_ENERGIZE: {"brightness": 1.00, "color": [255, 228, 206], "aim": AIM_UP, "camera": "on", "mic": "on",
                     "speaker": "on"},  # ~5000K daylight
}

# Servo aim presets — named device-head directions mapped to joint positions (normalized -100..100).
#
# These numbers are NOT angles. norm_mode is RANGE_M100_100 (use_degrees=False in
# config_hal_follower.py), so each one is a position on that joint's calibrated
# span: raw = ((v + 100) / 200) * (range_max - range_min) + range_min. That makes
# every value here a function of range_min/range_max in the calibration JSON —
# recalibrate a joint to a different span and the same number points somewhere else.
#
# Recaptured by hand on 21/08/2026, on the arm as recalibrated by
# `hal: recalibrate hal_follower servo ranges`: the head was driven to each pose
# from the Manual Move sliders in the web monitor and the slider values written
# down here.
#
# They were NOT converted from the previous set, and a future recalibration must
# not convert them either. Converting looks correct and is not: the obvious
# transform holds the raw encoder tick constant across the range change, but the
# servo reports Present_Position = Actual - Homing_Offset, and a hand
# recalibration rewrites Homing_Offset in the servo's own EEPROM. Same tick,
# different physical pose. That conversion was tried here first and produced a
# set where only left and right looked right — the two aims dominated by the one
# joint whose span barely moved (base_yaw), while every pitch was visibly off.
#
# So: after a recalibration, drive the head to each pose and write down what the
# sliders say. It takes eight poses and it is the only method that can be checked
# by looking at the lamp.
#
# left and right below are still the converted values and are known to be wrong;
# they are pending a hand capture like the rest.
AIM_PRESETS = {
    AIM_CENTER: {"base_yaw.pos": 0.0, "base_pitch.pos": 25.0, "elbow_pitch.pos": 43.0, "wrist_roll.pos": 10.0,
                 "wrist_pitch.pos": 30.0},
    AIM_DESK: {"base_yaw.pos": 0.6, "base_pitch.pos": 41.0, "elbow_pitch.pos": 42.9, "wrist_roll.pos": 9.2,
               "wrist_pitch.pos": 29.8},
    AIM_WALL: {"base_yaw.pos": 0.6, "base_pitch.pos": 19.0, "elbow_pitch.pos": 42.9, "wrist_roll.pos": 0.0,
               "wrist_pitch.pos": -6.0},
    AIM_LEFT: {"base_yaw.pos": -91.57, "base_pitch.pos": 2.62, "elbow_pitch.pos": 35.5, "wrist_roll.pos": 10.06,
               "wrist_pitch.pos": 52.07},
    AIM_RIGHT: {"base_yaw.pos": 88.36, "base_pitch.pos": 2.62, "elbow_pitch.pos": 35.5, "wrist_roll.pos": 10.06,
                "wrist_pitch.pos": 52.07},
    AIM_UP: {"base_yaw.pos": -0.4, "base_pitch.pos": 39.0, "elbow_pitch.pos": 65.0, "wrist_roll.pos": 8.9,
             "wrist_pitch.pos": 27.9},
    AIM_DOWN: {"base_yaw.pos": 0.0, "base_pitch.pos": 8.0, "elbow_pitch.pos": 15.0, "wrist_roll.pos": 5.0,
               "wrist_pitch.pos": -8.0},
    AIM_USER: {"base_yaw.pos": 0.0, "base_pitch.pos": 26.0, "elbow_pitch.pos": 33.0, "wrist_roll.pos": 10.0,
               "wrist_pitch.pos": -38.0},
}

# System status LED presets — the color/effect/speed the device shows for each
# os-server status state (booting, error, OTA, …). The os-server owns the state
# machine (WHEN to show a state); HAL owns the appearance (WHAT it looks like) so
# a device can restyle status feedback via presets.json without the OS knowing.
# Applied transiently via POST /led/status {state} (does not clobber user state).
# Keys MUST stay in sync with system/statusled State constants (Go) + the
# "ready_flash" agent-ready cue. Effects are from VALID_LED_EFFECTS.
# Brightness convention — read before changing a color here.
#
# These are INDICATORS, not illumination. Two rules keep them from glaring:
#
# 1. Stay BELOW the light.max_brightness ceiling. The safety gate (clamp_color)
#    scales a color so its peak channel meets the ceiling (lamp: 120), so any
#    channel written above the ceiling is dead value — [0,200,200] and
#    [0,120,120] look identical on lamp. Dimming must happen HERE, not there.
# 2. Equalize PERCEIVED brightness, not the numbers. Luminance is hue-weighted
#    (Rec.709: R 0.2126, G 0.7152, B 0.0722), so identical peaks are not
#    identically bright: at the 120 ceiling, cyan/yellow/white carry ~3-4x the
#    luminance of red/purple. Scaling every preset by the same factor preserves
#    that imbalance — which is what made agent_down (cyan, and a state that
#    stays lit for minutes) the one users complained about.
#
# Every cue below is tuned to relative luminance ~0.045, anchored on
# mic_muted (the one resting indicator already tuned on hardware and never
# reported as harsh). An earlier pass used ~0.12 and was still called glaring
# when viewed on a real lamp — in a dim room the strip sits close to the user's
# eyeline, so numbers that look modest on a monitor do not read that way there.
# Verify a change by eye on a device, not by arithmetic.
#
# Momentary flashes get the same treatment. Assuming a ~1s flash "cannot glare
# because it is brief" was also wrong on hardware: a full-value white flash is
# the harshest thing the strip does, and brevity does not soften it.
# PEAK BUDGET — the rule these values follow, derived by eye on a lamp
# (11/08/2026) after two model-driven passes both missed.
#
#   green-dominant hue (green, yellow, cyan, white) -> peak channel 12
#   little or no green (red, purple, orange, blue)   -> peak channel 16
#
# That is the whole rule. What it replaced, and why:
#
# Pass 1 wrote everything at 255 and let the light.max_brightness ceiling
# (lamp: 120) do the dimming. The gate scales a color so its PEAK channel meets
# the ceiling, so cyan landed at [0,120,120] while red landed at [120,0,0] —
# same clamp, wildly different brightness. Anything above the ceiling is also
# dead value: [0,200,200] and [0,120,120] are the same light.
#
# Pass 2 equalised Rec.709 relative luminance (R 0.2126, G 0.7152, B 0.0722).
# On hardware this overshot badly in both directions: it licensed red up to 54
# (clearly the brightest cue on the strip, though the maths called it dimmest)
# while holding green to 16 (still too bright). Rec.709 describes sRGB display
# primaries, not WS2812 dies, and it models linear light while the eye responds
# roughly to its cube root — so at these very low levels the weighting is wrong
# twice over. Do not reintroduce it.
#
# What survived testing is the green channel: it drives perceived brightness far
# more than red or blue, but nowhere near the 3.4x Rec.709 claims. Hence one
# small correction (12 vs 16) instead of a formula.
#
# Floor: do not go below peak ~8. The effect loop scales a color per frame and
# truncates (`int(c * brightness)`, effects.py), so a peak of 3 leaves the
# breathing cycle 4 distinct levels and the strip visibly steps.
#
# Tune by eye on a device. Both formulas above looked right on a monitor.
STATUS_LED_PRESETS = {
    "ota": {"effect": FX_BREATHING, "color": [0, 12, 0], "speed": 3.0},  # green — firmware updating
    # Red pulse, not breathing: mic_muted is also dim red breathing, so at these
    # levels a breathing red error was indistinguishable from "mic is muted" —
    # only the speed differed. Pulse separates them by shape, which reads
    # without having to count breaths, and suits a fault better anyway.
    "error": {"effect": FX_PULSE, "color": [16, 0, 0], "speed": 1.5},  # red — system error
    "booting": {"effect": FX_BREATHING, "color": [0, 6, 16], "speed": 3.0},  # blue — starting up
    "connectivity": {"effect": FX_BREATHING, "color": [16, 7, 0], "speed": 3.0},  # orange — no internet
    "wifi_connecting": {"effect": FX_BLINK, "color": [0, 6, 16], "speed": 0.5},
    # blue blink — associating with Wi-Fi during POST /api/device/setup
    "hal_down": {"effect": FX_BREATHING, "color": [11, 0, 16], "speed": 3.0},  # purple — HAL unreachable
    "agent_down": {"effect": FX_BREATHING, "color": [0, 12, 12], "speed": 3.0},  # cyan — agent disconnected
    "hardware": {"effect": FX_BREATHING, "color": [12, 12, 0], "speed": 3.0},  # yellow — hardware fault
    "ready_flash": {"effect": FX_NOTIFICATION_FLASH, "color": [12, 12, 12], "speed": 1.0},
    # white — agent ready/listening (brief, but a full-value white flash is the
    # harshest thing the strip does; brevity does not soften it)
    # OTA progress (driven by the bootstrap worker, not the statusled state machine)
    "ota_progress": {"effect": FX_BREATHING, "color": [16, 8, 0], "speed": 0.4},  # orange — updating
    "ota_error": {"effect": FX_PULSE, "color": [16, 2, 2], "speed": 1.5},  # red pulse — update failed
    "ota_success": {"effect": FX_NOTIFICATION_FLASH, "color": [0, 12, 4], "speed": 1.0},  # green flash — update ok
    # Setup/provisioning "device ready, join the AP" cue. effect "solid" = a
    # persistent fill (saved as the displayed state), not a transient overlay.
    # The one deliberate exception to the peak budget: white is green-dominant so
    # the rule says 12, but this cue has to be spotted across a room by someone
    # asking "is it on yet?" during onboarding, so it gets the 16 tier instead.
    "setup": {"effect": "solid", "color": [16, 16, 16], "speed": 1.0},  # white solid — AP/setup ready
    # Mic-muted idle indicator — HAL-local key (no Go statusled state). The
    # strip's RESTING look while the mic is muted: emotions/effects/waves run
    # normally on top, and every LED restore lands back on this instead of the
    # user state, so "nothing happening + red breathing" = mic is muted.
    # Applied by /voice/mute, cleared by /voice/unmute (app_state._mic_muted_led).
    # Deliberately dimmer than the light.max_brightness ceiling would force
    # (the gate alone would clamp 140 -> 120): this is a RESTING look that
    # stays lit for as long as the mic is muted, often pointed at the user, so
    # it is tuned for "glanceable", not "bright". Red-only helps — at the same
    # value it carries ~1/4 the luminance of white. Don't go much lower or the
    # privacy indicator stops reading in a daylit room.
    "mic_muted": {"effect": FX_BREATHING, "color": [10, 0, 0], "speed": 0.8},  # dark red — mic muted
}

# Button hold-warning LEDs — what the strip shows WHILE a physical button is
# held, one color per armed tier (see hal/drivers/button_actions.py for the
# hold thresholds and hal/drivers/gpio_button.py for the blink/solid staging).
# Same peak budget as STATUS_LED_PRESETS (16 for these low-green hues): writing
# 255 here would not make them "bright but safe" — clamp_color only scales a
# color's PEAK up to the light.max_brightness ceiling (lamp: 120), so dimming
# has to happen here. Purple identifies the sleep tier; blink vs solid separates
# shutdown from factory-reset, so those two share a color on purpose.
#
# A dict rather than three module constants so devices can restyle these through
# presets.json like every other LED table. The overlay merges tables IN PLACE
# (board/presets_overlay.py), which only reaches readers that go through the
# dict at call time — `from hal.presets import LED_SLEEP_WARN` would bind the
# value at import and silently ignore the override.
BUTTON_LED_PRESETS = {
    "sleep_warn": {"color": [8, 5, 16]},  # sleepy purple (blinking) — hold 2-5s
    "shutdown_warn": {"color": [16, 0, 0]},  # red (blinking) — hold 5-10s
    "factory_reset": {"color": [16, 0, 0]},  # red (solid) — hold 10s+
}

# Backend-error flash (app_state._flash_backend_error) — one notification_flash
# when an agent API call fails. Amber-yellow, deliberately a different hue from
# the statusled "hardware" fault cue so the two do not read as the same thing.
# Same peak budget as everything else here (12, green-dominant).
LED_BACKEND_ERROR_FLASH = (12, 9, 0)

# Ambient resting look — what the strip settles into when no user LED state
# exists. MUST mirror the Go ambient fallback (system/ambient/service.go
# `ambientRestingColor`) so a HAL-side settle (e.g. mic unmute with no saved
# state) is visually identical to what ambient paints on idle. Flip the two
# together or the device shows one look on idle and another on restore.
#
# A BLACK color means "resting state is dark": the settle paths clear the strip
# instead of starting an effect (an effect thread breathing black would burn
# 25 fps of SPI writes and make GET /led/color report on=true over a dark
# strip). Light then becomes opt-in — it comes on for an action (emotion,
# status cue, explicit user/agent color) and goes back to black when that
# action releases the strip.
#
# Currently [0, 0, 0] — default off, per the 30/07/2026 product call: the lamp
# was lighting itself up unasked and shining into users' faces. Restore
# [255, 200, 140] (warm white ~2700K @ speed 0.3) to bring back the previous
# "a lamp at rest reads as a cozy lamp turned on" behavior.
AMBIENT_RESTING_LED = {"effect": FX_BREATHING, "color": [0, 0, 0], "speed": 0.3}


def ambient_resting_is_dark() -> bool:
    """True when the resting look is black, i.e. the strip's idle state is OFF.
    Settle paths use this to clear the strip instead of running an effect."""
    return not any(AMBIENT_RESTING_LED.get("color") or [0, 0, 0])
