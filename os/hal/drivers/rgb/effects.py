"""
LED effect loops — each function runs in a background thread until stop_event is set
or deadline is reached. All effects accept (color, speed, deadline, stop_event, svc)
except where noted (rainbow omits color; notification_flash omits deadline).
"""

import math
import random
import time
import threading
from typing import Optional

from hal.presets import (
    FX_BLINK, FX_BREATHING, FX_CANDLE, FX_NOTIFICATION_FLASH,
    FX_PULSE, FX_RAINBOW, FX_SPEAKING_WAVE, FX_SPEAKING_WAVE_RAINBOW,
    RGB_CMD_PAINT, RGB_CMD_SOLID,
)
from hal.board.presets_overlay import DEFAULT_LED_COUNT


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
):
    """Dispatch to the appropriate effect loop. Runs in a background thread."""
    deadline = None
    if duration_ms is not None:
        deadline = time.monotonic() + duration_ms / 1000.0

    try:
        if effect == FX_BREATHING:
            breathing(color, speed, deadline, stop_event, svc)
        elif effect == FX_CANDLE:
            candle(color, speed, deadline, stop_event, svc)
        elif effect == FX_RAINBOW:
            rainbow(speed, deadline, stop_event, svc)
        elif effect == FX_NOTIFICATION_FLASH:
            notification_flash(color, speed, stop_event, svc)
        elif effect == FX_PULSE:
            pulse(color, speed, deadline, stop_event, svc, base_color or (0, 0, 0))
        elif effect == FX_BLINK:
            blink(color, speed, deadline, stop_event, svc)
        elif effect == FX_SPEAKING_WAVE:
            speaking_wave(color, speed, deadline, stop_event, svc)
        elif effect == FX_SPEAKING_WAVE_RAINBOW:
            speaking_wave_rainbow(speed, deadline, stop_event, svc)
    except Exception as e:
        import logging
        logging.getLogger("hal.led.effects").warning("LED effect '%s' error: %s", effect, e)


def breathing(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
):
    """Fade in/out with the given color."""
    step_delay = 0.03 / speed
    while not is_done(deadline, stop_event):
        # Full cycle: 0 -> 1 -> 0 over ~3s at speed=1
        for i in range(100):
            if is_done(deadline, stop_event):
                return
            brightness = math.sin(math.pi * i / 100.0)
            scaled = tuple(int(c * brightness) for c in color)
            svc.dispatch(RGB_CMD_SOLID, scaled)
            time.sleep(step_delay)


def candle(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
):
    """Warm flicker effect with randomized warm tones."""
    step_delay = 0.05 / speed
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    while not is_done(deadline, stop_event):
        pixels = []
        for _ in range(led_count):
            flicker = random.uniform(0.4, 1.0)
            # Warm tone bias: keep red high, vary green, minimal blue
            r = int(min(255, color[0] * flicker + random.randint(0, 20)))
            g = int(min(255, color[1] * flicker * random.uniform(0.6, 0.9)))
            b = int(min(255, color[2] * flicker * 0.3))
            pixels.append((r, g, b))
        svc.dispatch(RGB_CMD_PAINT, pixels)
        time.sleep(step_delay)


def rainbow(
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
):
    """Cycle through hue spectrum across all pixels."""
    step_delay = 0.03 / speed
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    offset = 0.0
    while not is_done(deadline, stop_event):
        pixels = []
        for i in range(led_count):
            hue = (offset + i / led_count) % 1.0
            r, g, b = hsv_to_rgb(hue, 1.0, 1.0)
            pixels.append((r, g, b))
        svc.dispatch(RGB_CMD_PAINT, pixels)
        offset += 0.01
        time.sleep(step_delay)


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
        time.sleep(flash_on)
        if stop_event.is_set():
            return
        svc.dispatch(RGB_CMD_SOLID, (0, 0, 0))
        time.sleep(flash_off)


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
        time.sleep(half_period)
        if is_done(deadline, stop_event):
            return
        svc.dispatch(RGB_CMD_SOLID, (0, 0, 0))
        time.sleep(half_period)


def pulse(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
    base_color: tuple = (0, 0, 0),
):
    """Ripple pulse overlaid on a base color.

    Pixels outside the wavefront stay at base_color; pixels at the wavefront
    blend toward `color` by a falloff factor. Killing the effect mid-frame
    leaves the strip showing base_color + a fading ripple instead of a
    half-painted dark frame.
    """
    step_delay = 0.04 / speed
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    center = led_count // 2
    max_radius = center + 1
    while not is_done(deadline, stop_event):
        for radius in range(max_radius + 1):
            if is_done(deadline, stop_event):
                return
            pixels = [base_color] * led_count
            for i in range(led_count):
                dist = abs(i - center)
                if dist <= radius:
                    falloff = max(
                        0.0, 1.0 - abs(dist - radius) / max(max_radius * 0.3, 1)
                    )
                    if falloff > 0:
                        pixels[i] = tuple(
                            int(base_color[c] + (color[c] - base_color[c]) * falloff)
                            for c in range(3)
                        )
            svc.dispatch(RGB_CMD_PAINT, pixels)
            time.sleep(step_delay)


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
        time.sleep(step_delay)


def speaking_wave_rainbow(
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
):
    """Same VU-meter motion as speaking_wave, but each segment paints a
    different hue (rainbow palette) that slowly drifts over time. Used when
    the user hasn't set an LED color but music is playing.
    """
    step_delay = 0.04 / speed
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    num_segments = 8
    seg_size = led_count // num_segments

    current = [0.5] * num_segments
    target = [random.uniform(0.2, 1.0) for _ in range(num_segments)]
    frames_until_new_target = 0
    hue_offset = 0.0

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
            r, g, b = hsv_to_rgb(hue, 1.0, brightness)
            seg_color = (r, g, b)
            for p in range(seg_size):
                idx = s * seg_size + p
                if idx < led_count:
                    pixels[idx] = seg_color

        svc.dispatch(RGB_CMD_PAINT, pixels)
        hue_offset = (hue_offset + 0.005) % 1.0
        time.sleep(step_delay)
