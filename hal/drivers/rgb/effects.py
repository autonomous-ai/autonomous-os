"""
LED effect loops — each function runs in a background thread until stop_event is set
or deadline is reached. All effects accept (color, speed, deadline, stop_event, svc)
except where noted (rainbow has no color and takes a `brightness` level
instead; notification_flash omits deadline).
"""

import math
import random
import time
import threading
from typing import Optional

from hal.presets import (
    FX_BLINK, FX_BREATHING, FX_BREATHING_FINE, FX_CANDLE, FX_NOTIFICATION_FLASH,
    FX_PULSE, FX_RAINBOW, FX_SPEAKING_WAVE, FX_SPEAKING_WAVE_RAINBOW,
    RGB_CMD_PAINT, RGB_CMD_SOLID,
)
from hal.board.presets_overlay import DEFAULT_LED_COUNT

# --- candle flicker, bounded by IEEE 1789-2015 ---------------------------
#
# The old candle picked a fresh random level per pixel every frame, at
# 0.05/speed — 4 Hz for happy/laugh/confused, 10 Hz for excited — with each
# pixel stepping independently between 0.4 and 1.0 of the base color. That is
# a 60% modulation depth at 4-10 Hz, and IEEE 1789-2015 puts ANY flicker below
# 90 Hz in its high-risk band (at 100 Hz the limit is already 1.6%). The
# 3-70 Hz range is the one associated with headaches and visual discomfort,
# and on a desk lamp pointed at a face it was reported as dizzying.
#
# Two changes bring it inside the guidance without changing what it is:
#
#   * Refresh at a fixed 30 Hz and INTERPOLATE toward each pixel's target
#     instead of jumping to it. Steps are what the eye locks onto; a smooth
#     ramp of the same period reads as motion, not flicker.
#   * Raise the floor from 0.4 to 0.8, cutting modulation depth from 60% to
#     20%. The flame still lives, it just stops strobing.
#
# Do not lower CANDLE_FLICKER_MIN or CANDLE_REFRESH_HZ without re-reading the
# standard — this is a comfort/safety bound, not a taste setting.
CANDLE_FLICKER_MIN = 0.8
CANDLE_REFRESH_HZ = 30


def is_done(deadline: Optional[float], stop_event: threading.Event) -> bool:
    """Return True if the effect should stop."""
    if stop_event.is_set():
        return True
    if deadline is not None and time.monotonic() >= deadline:
        return True
    return False


def hsv_to_rgb(h: float, s: float, v: float) -> tuple:
    """Convert HSV (0-1 range) to RGB (0-255 ints)."""
    if s == 0.0:
        val = int(v * 255)
        return (val, val, val)
    i = int(h * 6.0)
    f = (h * 6.0) - i
    p = int(255 * v * (1.0 - s))
    q = int(255 * v * (1.0 - s * f))
    t = int(255 * v * (1.0 - s * (1.0 - f)))
    v_int = int(255 * v)
    i %= 6
    if i == 0:
        return (v_int, t, p)
    if i == 1:
        return (q, v_int, p)
    if i == 2:
        return (p, v_int, t)
    if i == 3:
        return (p, q, v_int)
    if i == 4:
        return (t, p, v_int)
    return (v_int, p, q)


def run_effect(
    effect: str,
    color: tuple,
    speed: float,
    duration_ms: Optional[int],
    stop_event: threading.Event,
    svc,
    base_color: Optional[tuple] = None,
    brightness: float = 1.0,
    start_at_peak: bool = False,
):
    """Dispatch to the appropriate effect loop. Runs in a background thread.

    Effect loops must pace frames with stop_event.wait(delay), never
    time.sleep(delay): _stop_current_effect() joins this thread, and a
    plain sleep makes that join block for up to a full frame/segment
    delay (seconds for slow-speed blink/flash — past the 2s join timeout,
    which leaks a zombie effect that keeps painting over new colors).
    """
    deadline = None
    if duration_ms is not None:
        deadline = time.monotonic() + duration_ms / 1000.0

    try:
        if effect == FX_BREATHING:
            breathing(color, speed, deadline, stop_event, svc, start_at_peak)
        elif effect == FX_BREATHING_FINE:
            breathing_fine(color, speed, deadline, stop_event, svc, start_at_peak)
        elif effect == FX_CANDLE:
            candle(color, speed, deadline, stop_event, svc)
        elif effect == FX_RAINBOW:
            rainbow(speed, deadline, stop_event, svc, brightness)
        elif effect == FX_NOTIFICATION_FLASH:
            notification_flash(color, speed, stop_event, svc)
        elif effect == FX_PULSE:
            pulse(color, speed, deadline, stop_event, svc, base_color or (0, 0, 0))
        elif effect == FX_BLINK:
            blink(color, speed, deadline, stop_event, svc)
        elif effect == FX_SPEAKING_WAVE:
            speaking_wave(color, speed, deadline, stop_event, svc)
        elif effect == FX_SPEAKING_WAVE_RAINBOW:
            speaking_wave_rainbow(speed, deadline, stop_event, svc, brightness)
    except Exception as e:
        import logging
        logging.getLogger("hal.led.effects").warning("LED effect '%s' error: %s", effect, e)


def breathing(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
    start_at_peak: bool = False,
):
    """Fade in/out with the given color.

    start_at_peak skips the first half of the opening arc so the very first
    frame is full brightness, and only then breathes down into the normal
    cycle. It exists for cues that answer the user NOW: the arc's rise is slow
    (a full 0 -> 1 -> 0 takes 10s at speed=0.3) and `int()` truncation on a dim
    preset holds the output at literal (0,0,0) for the first second or so of
    it, so an acknowledgement painted this way is invisible exactly when it is
    supposed to be read (device-observed, 28/8: the listening cue fired on the
    first STT partial but was not seen until the last one). Off by default —
    an emotion that fades in is meant to fade in. Note the equivalent fix
    cannot live at the call site: dispatching the colour before starting this
    thread is erased milliseconds later by the i=0 frame below.
    """
    step_delay = 0.03 / speed
    # Only the OPENING arc starts at the peak; every cycle after it breathes
    # from 0 as usual.
    start = 50 if start_at_peak else 0
    while not is_done(deadline, stop_event):
        # Full cycle: 0 -> 1 -> 0 over ~3s at speed=1
        for i in range(start, 100):
            if is_done(deadline, stop_event):
                return
            brightness = math.sin(math.pi * i / 100.0)
            scaled = tuple(int(c * brightness) for c in color)
            svc.dispatch(RGB_CMD_SOLID, scaled)
            stop_event.wait(step_delay)
        start = 0


# Spread the fractional level across the ring, one unit at a time. `k` of the
# pixels sit one step above the rest and are scattered with a stride coprime to
# the ring size, so the strip reads as one ring at an in-between level instead
# of a lit arc. 13 and 32 are coprime; DEFAULT_LED_COUNT is a power of two on
# every board shipped so far, and any odd stride stays coprime with it.
_DITHER_STRIDE = 13


def _dither_ring(low: tuple, high: tuple, k: int, n: int) -> list:
    """n pixels, k of them at `high` and the rest at `low`, evenly scattered."""
    if k <= 0:
        return [low] * n
    if k >= n:
        return [high] * n
    return [high if (i * _DITHER_STRIDE) % n < k else low for i in range(n)]


def breathing_fine(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
    start_at_peak: bool = False,
):
    """Breathing whose resolution comes from the ring, not from 8-bit colour.

    Plain `breathing` scales the colour per frame and truncates, which is fine
    at a peak of 200 and useless at a peak of 2: the only values reachable are
    0, 1 and 2, the top one lasts a single frame, and the strip spends a third
    of every cycle at literal black. On the lamp that turned the listening cue
    into a blink followed by darkness — the opposite of what the preset asks
    for ("stays lit for as long as the user is talking"), and the reason the
    glare passes could not simply be undone: raising the peak was rejected on
    device (28/08/2026), so the levels had to come from somewhere else.

    Here the breath moves between `color` and one unit below it, and the
    fraction in between is rendered by raising SOME of the pixels — the eye
    integrates the ring, so a 32-pixel strip gains ~32 sub-levels per unit
    while every pixel stays lit and no pixel ever exceeds `color`. Peak
    brightness is therefore identical to plain breathing; only the floor and
    the resolution change.
    """
    step_delay = 0.03 / speed
    n = getattr(svc, "led_count", DEFAULT_LED_COUNT) or DEFAULT_LED_COUNT
    # One unit below the peak on every channel that is lit: the floor of the
    # breath. Channels that are already 0 stay 0, so the hue never shifts.
    low = tuple(max(c - 1, 0) for c in color)
    start = 50 if start_at_peak else 0
    while not is_done(deadline, stop_event):
        for i in range(start, 100):
            if is_done(deadline, stop_event):
                return
            brightness = math.sin(math.pi * i / 100.0)
            svc.dispatch(RGB_CMD_PAINT, _dither_ring(low, color, round(brightness * n), n))
            stop_event.wait(step_delay)
        start = 0


def candle(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
):
    """Flicker effect: per-pixel brightness varies, hue does NOT.

    Every pixel is the SAME color scaled by its own flicker factor, so the
    strip reads as one color breathing unevenly — a flame — instead of a
    handful of different colors.

    It used to shape each channel separately (``+ randint(0, 20)`` on red, a
    ``x0.6-0.9`` squeeze on green, ``x0.3`` on blue). That was written when
    presets ran near full scale, where a 20-count nudge is a faint warm
    shimmer. Emotion presets now peak at 12-16 (they are indicators, not
    illumination — see EMOTION_PRESETS), and at that scale the additive term
    is LARGER than the color itself: happy [12, 9, 1] came out anywhere from
    (9, 5, 0) to (26, 4, 0), i.e. red at 2.2x its own value while green was
    squeezed. Hue swung from the declared 44 deg (yellow) down to 5-20 deg,
    so the lamp showed a scatter of orange pixels and never the color the
    preset asked for (observed on a lamp, 19/08/2026). excited [12, 8, 12]
    fared worst: blue crushed, red inflated, its pink-purple read as orange.

    Scaling all three channels by one factor keeps the ratio — and therefore
    the hue — identical at any brightness, which is the same rule the presets
    themselves follow ("dim by scaling, never by picking a new color").
    """
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    step_delay = 1.0 / CANDLE_REFRESH_HZ
    # Fraction of the remaining distance a pixel covers each frame. `speed`
    # keeps its old meaning (higher = livelier flame) but now sets how fast a
    # pixel travels toward its target instead of how often it teleports.
    # 0.6 measured against the presets in use: the flame moves at 0.85 Hz
    # (speed 0.2) to 2.25 Hz (excited, speed 0.5), so even the liveliest one
    # stays under the 3 Hz start of the uncomfortable band. Raising this puts
    # excited back into it.
    approach = min(1.0, max(0.02, speed * 0.6))
    levels = [random.uniform(CANDLE_FLICKER_MIN, 1.0) for _ in range(led_count)]
    targets = [random.uniform(CANDLE_FLICKER_MIN, 1.0) for _ in range(led_count)]
    while not is_done(deadline, stop_event):
        pixels = []
        for i in range(led_count):
            level = levels[i] + (targets[i] - levels[i]) * approach
            levels[i] = level
            if abs(targets[i] - level) < 0.01:
                targets[i] = random.uniform(CANDLE_FLICKER_MIN, 1.0)
            pixels.append(tuple(min(255, int(c * level)) for c in color))
        svc.dispatch(RGB_CMD_PAINT, pixels)
        stop_event.wait(step_delay)


def rainbow(
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
    brightness: float = 1.0,
):
    """Cycle through hue spectrum across all pixels.

    rainbow has no color parameter — it sweeps the hue circle by definition —
    so it takes its LEVEL from the preset's `brightness` (0.0-1.0, the same
    field the scene table uses). Before this it always painted at 1.0 and was
    the one cue no per-device palette could dim: it rode straight up to the
    light.max_brightness ceiling on every pixel at once.
    """
    step_delay = 0.03 / speed
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    value = max(0.0, min(1.0, brightness))
    offset = 0.0
    while not is_done(deadline, stop_event):
        pixels = []
        for i in range(led_count):
            hue = (offset + i / led_count) % 1.0
            r, g, b = hsv_to_rgb(hue, 1.0, value)
            pixels.append((r, g, b))
        svc.dispatch(RGB_CMD_PAINT, pixels)
        offset += 0.01
        stop_event.wait(step_delay)


def notification_flash(
    color: tuple,
    speed: float,
    stop_event: threading.Event,
    svc,
):
    """3 quick flashes then stop."""
    flash_on = 0.15 / speed
    flash_off = 0.1 / speed
    for _ in range(3):
        if stop_event.is_set():
            return
        svc.dispatch(RGB_CMD_SOLID, color)
        stop_event.wait(flash_on)
        if stop_event.is_set():
            return
        svc.dispatch(RGB_CMD_SOLID, (0, 0, 0))
        stop_event.wait(flash_off)


def blink(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
):
    """Rapid on/off blink. speed=1 → ~3 Hz, speed=2 → ~6 Hz, speed=0.5 → ~1.5 Hz."""
    half_period = 1.0 / (speed * 6.0)  # on time = off time
    while not is_done(deadline, stop_event):
        svc.dispatch(RGB_CMD_SOLID, color)
        stop_event.wait(half_period)
        if is_done(deadline, stop_event):
            return
        svc.dispatch(RGB_CMD_SOLID, (0, 0, 0))
        stop_event.wait(half_period)


def pulse(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
    base_color: tuple = (0, 0, 0),
):
    """A pulse of light travelling around the ring, overlaid on a base color.

    Pixels away from the wavefront stay at base_color; pixels near it blend
    toward `color` by a falloff factor, so the pulse has a bright head and a
    short tail. Killing the effect mid-frame leaves base_color plus a fading
    wave rather than a half-painted dark frame.

    The wavefront travels: it used to sit at a fixed origin and expand outward,
    which on a closed ring is two arcs opening in opposite directions — never
    the one moving light the effect is named for. (That it ever looked right on
    the lamp was an accident of led_count being declared twice the true ring
    size, which put the origin off the end of the strip so only one arc showed.)
    """
    step_delay = 0.04 / speed
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    # How much of the ring the pulse covers. A sixth reads as a distinct moving
    # light: wide enough to have a tail, narrow enough to leave the ring dark
    # ahead of it.
    width = max(2.0, led_count / 6.0)
    while not is_done(deadline, stop_event):
        for head in range(led_count):
            if is_done(deadline, stop_event):
                return
            pixels = [base_color] * led_count
            for i in range(led_count):
                delta = abs(i - head)
                # Distance the short way round — pixel 0 neighbours the last one.
                dist = min(delta, led_count - delta)
                falloff = max(0.0, 1.0 - dist / width)
                if falloff > 0:
                    pixels[i] = tuple(
                        int(base_color[c] + (color[c] - base_color[c]) * falloff)
                        for c in range(3)
                    )
            svc.dispatch(RGB_CMD_PAINT, pixels)
            stop_event.wait(step_delay)


def speaking_wave(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
):
    """Audio-reactive speaking effect — simulated VU meter / equalizer.

    Divides the LED strip into 8 segments. Each segment has its own
    brightness target that changes randomly every few frames, simulating
    audio amplitude response. Brightness smoothly interpolates toward
    targets to avoid harsh flickering. Looks like the device is "reacting"
    to its own speech.
    """
    step_delay = 0.04 / speed  # ~25fps
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    num_segments = 8
    seg_size = led_count // num_segments

    # Each segment has a current brightness and a target brightness
    current = [0.5] * num_segments
    target = [random.uniform(0.2, 1.0) for _ in range(num_segments)]
    frames_until_new_target = 0

    while not is_done(deadline, stop_event):
        # Pick new random targets every 4-8 frames (~160-320ms)
        if frames_until_new_target <= 0:
            for s in range(num_segments):
                target[s] = random.uniform(0.0, 1.0)
            frames_until_new_target = random.randint(4, 8)
        frames_until_new_target -= 1

        # Smooth interpolation toward targets
        for s in range(num_segments):
            current[s] += (target[s] - current[s]) * 0.3

        # Paint pixels
        pixels = [(0, 0, 0)] * led_count
        for s in range(num_segments):
            brightness = current[s]
            seg_color = tuple(int(c * brightness) for c in color)
            for p in range(seg_size):
                idx = s * seg_size + p
                if idx < led_count:
                    pixels[idx] = seg_color

        svc.dispatch(RGB_CMD_PAINT, pixels)
        stop_event.wait(step_delay)


def speaking_wave_rainbow(
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
    brightness: float = 1.0,
):
    """Same VU-meter motion as speaking_wave, but each segment paints a
    different hue (rainbow palette) that slowly drifts over time. Used when
    the user hasn't set an LED color but music is playing.

    Like rainbow(), it generates its own color, so `brightness` (0.0-1.0) is
    the only level it has: the VU envelope rides UNDER it, peaking at
    255 * brightness. There are two rainbow cues around music and they come
    from different places — the agent emits the music_strong emotion via
    skills/emotion (that one is rainbow()), while THIS one is lit by
    _on_music_play_start for the whole length of a song. Both read the same
    music_strong "brightness", so a device dims them with one number.
    """
    step_delay = 0.04 / speed
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    num_segments = 8
    seg_size = led_count // num_segments

    current = [0.5] * num_segments
    target = [random.uniform(0.2, 1.0) for _ in range(num_segments)]
    frames_until_new_target = 0
    hue_offset = 0.0
    level = max(0.0, min(1.0, brightness))

    while not is_done(deadline, stop_event):
        if frames_until_new_target <= 0:
            for s in range(num_segments):
                target[s] = random.uniform(0.0, 1.0)
            frames_until_new_target = random.randint(4, 8)
        frames_until_new_target -= 1

        for s in range(num_segments):
            current[s] += (target[s] - current[s]) * 0.3

        pixels = [(0, 0, 0)] * led_count
        for s in range(num_segments):
            brightness = current[s]
            hue = (hue_offset + s / num_segments) % 1.0
            r, g, b = hsv_to_rgb(hue, 1.0, brightness * level)
            seg_color = (r, g, b)
            for p in range(seg_size):
                idx = s * seg_size + p
                if idx < led_count:
                    pixels[idx] = seg_color

        svc.dispatch(RGB_CMD_PAINT, pixels)
        hue_offset = (hue_offset + 0.005) % 1.0
        stop_event.wait(step_delay)
