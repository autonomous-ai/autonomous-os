"""
LeLamp presets — emotion, scene, and LED effect constants.

All pure data, no runtime dependencies. Import from server.py.
"""

# --- Language code constants (Lumi stt_language / TTS language) ---
# Keep these aligned with the language codes returned by /lumi config and
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
FX_CANDLE = "candle"
FX_RAINBOW = "rainbow"
FX_NOTIFICATION_FLASH = "notification_flash"
FX_PULSE = "pulse"
FX_BLINK = "blink"
FX_SPEAKING_WAVE = "speaking_wave"
FX_SPEAKING_WAVE_RAINBOW = "speaking_wave_rainbow"

VALID_LED_EFFECTS = [FX_BREATHING, FX_CANDLE, FX_RAINBOW, FX_NOTIFICATION_FLASH, FX_PULSE, FX_BLINK, FX_SPEAKING_WAVE, FX_SPEAKING_WAVE_RAINBOW]

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
# "camera": "off" = auto-disable camera (e.g. sleepy — lamp going to sleep)
# "camera": "on"  = auto-enable camera if off (active interaction, need vision)
# omitted         = no camera change
EMOTION_PRESETS = {
    EMO_CURIOUS:       {"servo": SERVO_CURIOUS,       "color": [255, 191, 0],   "effect": FX_BREATHING,          "speed": 1.0, "camera": "on"},
    EMO_HAPPY:         {"servo": SERVO_HAPPY_WIGGLE,  "color": [255, 220, 0],   "effect": FX_CANDLE,             "speed": 1.0, "camera": "on"},
    EMO_SAD:           {"servo": SERVO_SAD,           "color": [80, 80, 200],   "effect": FX_BREATHING,          "speed": 0.8, "camera": "on"},
    EMO_THINKING:      {"servo": SERVO_THINKING_DEEP, "color": [180, 100, 255], "effect": FX_PULSE,              "speed": 1.5, "camera": "on"},
    EMO_IDLE:          {"servo": SERVO_IDLE,          "color": [183, 235, 234], "effect": FX_BREATHING,          "speed": 0.8},
    EMO_EXCITED:       {"servo": SERVO_EXCITED,       "color": [230, 51, 230],  "effect": FX_BLINK,              "speed": 2.5, "camera": "on"},
    EMO_SHY:           {"servo": SERVO_SHY,           "color": [255, 150, 180], "effect": FX_BLINK,              "speed": 0.5, "camera": "on"},
    EMO_SHOCK:         {"servo": SERVO_SHOCK,         "color": [255, 255, 255], "effect": FX_NOTIFICATION_FLASH, "speed": 2.0, "camera": "on"},
    EMO_LISTENING:     {"servo": SERVO_LISTENING,     "color": [51, 121, 230],  "effect": FX_PULSE,              "speed": 1.5, "camera": "on"},
    EMO_LAUGH:         {"servo": SERVO_LAUGH,         "color": [230, 191, 51],  "effect": FX_BLINK,              "speed": 1.2, "camera": "on"},
    EMO_CONFUSED:      {"servo": SERVO_CONFUSED,      "color": [224, 71, 25],   "effect": FX_CANDLE,             "speed": 0.6, "camera": "on"},
    EMO_SLEEPY:        {"servo": SERVO_SLEEPY,        "color": [60, 40, 120],   "effect": FX_BREATHING,          "speed": 0.5, "camera": "off"},
    EMO_GREETING:      {"servo": SERVO_GREETING,      "color": [255, 180, 100], "effect": FX_BLINK,              "speed": 0.8, "camera": "on"},
    EMO_GOODBYE:       {"servo": SERVO_GOODBYE,       "color": [255, 180, 100], "effect": FX_BREATHING,          "speed": 0.5},
    EMO_CARING:        {"servo": SERVO_NOD,           "color": [255, 160, 120], "effect": FX_BREATHING,          "speed": 0.4, "camera": "on"},
    EMO_ACKNOWLEDGE:   {"servo": SERVO_ACKNOWLEDGE,   "color": [51, 230, 141],  "effect": FX_BLINK,              "speed": 1.0, "camera": "on"},
    EMO_STRETCHING:    {"servo": SERVO_STRETCHING,    "color": [245, 240, 230], "effect": FX_BREATHING,          "speed": 0.6, "camera": "on"},
    EMO_MUSIC_STRONG:  {"servo": SERVO_MUSIC_ROCK,    "color": [155, 221, 155], "effect": FX_RAINBOW,            "speed": 1.5},
    EMO_MUSIC_CHILL:   {"servo": SERVO_MUSIC_ROCK,    "color": [252, 136, 3],   "effect": FX_BREATHING,          "speed": 0.5},
    EMO_SCAN:          {"servo": SERVO_SCANNING,      "color": [36, 184, 224],  "effect": FX_PULSE,              "speed": 2.0, "camera": "on"},
    EMO_NOD:           {"servo": SERVO_NOD,           "color": [51, 230, 141],  "effect": FX_BLINK,              "speed": 1.0, "camera": "on"},
    EMO_HEADSHAKE:     {"servo": SERVO_HEADSHAKE,     "color": [230, 51, 51],   "effect": FX_BLINK,              "speed": 1.0, "camera": "on"},
}

# Lighting scene presets — simulated color temperature via RGB mixing.
# 2200K = very warm amber, 2700K = warm white, 4000K = neutral, 5000K = cool, 6500K = daylight
# "camera": "off"/"on" = auto-disable/enable camera
# "mic": "off"/"on"    = mute/unmute microphone
# "speaker": "off"/"on"= mute/unmute speaker
# "servo": "hold"       = freeze servo (no idle/emotion animations)
# omitted               = no change for that peripheral
SCENE_PRESETS = {
    SCENE_READING:  {"brightness": 0.80, "color": [255, 209, 163], "aim": AIM_DESK, "camera": "off", "mic": "on",  "speaker": "off", "servo": "hold"},  # ~4000K neutral; mic on for voice wake
    SCENE_FOCUS:    {"brightness": 0.70, "color": [255, 214, 170], "aim": AIM_DESK, "camera": "off", "mic": "on",  "speaker": "off", "servo": "hold"},  # ~4200K warm-neutral; mic on for voice wake
    SCENE_RELAX:    {"brightness": 0.40, "color": [255, 166, 87],  "aim": AIM_WALL, "camera": "on",  "mic": "on",  "speaker": "on"},                    # ~2700K warm
    SCENE_MOVIE:    {"brightness": 0.15, "color": [255, 147, 51],  "aim": AIM_WALL, "camera": "off", "mic": "on",  "speaker": "off"},                   # ~2400K dim amber
    SCENE_NIGHT:    {"brightness": 0.05, "color": [255, 105, 0],   "aim": AIM_DOWN, "camera": "off", "mic": "on",  "speaker": "off"},                   # ~1800K deep amber, blue-free; mic stays on for voice wake
    SCENE_ENERGIZE: {"brightness": 1.00, "color": [255, 228, 206], "aim": AIM_UP,   "camera": "on",  "mic": "on",  "speaker": "on"},                    # ~5000K daylight
}

# Servo aim presets — named lamp-head directions mapped to joint positions (normalized -100..100).
# Neutral: base_yaw=3, base_pitch=-30, elbow_pitch=57, wrist_roll=0, wrist_pitch=18
AIM_PRESETS = {
    AIM_CENTER: {"base_yaw.pos": 3.0,   "base_pitch.pos": -20.0, "elbow_pitch.pos": 32.0, "wrist_roll.pos": 0.0, "wrist_pitch.pos": 0.0},
    AIM_DESK:   {"base_yaw.pos": 3.0,   "base_pitch.pos": 5.0,   "elbow_pitch.pos": 20.0, "wrist_roll.pos": 0.0, "wrist_pitch.pos": 40.0},
    AIM_WALL:   {"base_yaw.pos": 3.0,   "base_pitch.pos": 5.0,   "elbow_pitch.pos": -20.0, "wrist_roll.pos": 0.0, "wrist_pitch.pos": -60.0},
    AIM_LEFT:   {"base_yaw.pos": -90.0, "base_pitch.pos": -30.0, "elbow_pitch.pos": 57.0, "wrist_roll.pos": 0.0, "wrist_pitch.pos": 18.0},
    AIM_RIGHT:  {"base_yaw.pos": 90.0,  "base_pitch.pos": -30.0, "elbow_pitch.pos": 57.0, "wrist_roll.pos": 0.0, "wrist_pitch.pos": 18.0},
    AIM_UP:     {"base_yaw.pos": 3.0,   "base_pitch.pos": 10.0,  "elbow_pitch.pos": -15.0,  "wrist_roll.pos": 0.0, "wrist_pitch.pos": 25.0},
    AIM_DOWN:   {"base_yaw.pos": 3.0,   "base_pitch.pos": -90.0, "elbow_pitch.pos": 90.0, "wrist_roll.pos": 0.0, "wrist_pitch.pos": -90.0},
    AIM_USER:   {"base_yaw.pos": 0.0,   "base_pitch.pos": 0.0,   "elbow_pitch.pos": 0.0,  "wrist_roll.pos": 0.0, "wrist_pitch.pos": -45.0},
}
