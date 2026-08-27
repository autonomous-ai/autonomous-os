"""LED route handlers -- all /led/* endpoints."""

import threading
from typing import Optional

from fastapi import APIRouter, Body, HTTPException

import hal.app_state as state
from hal.models import (
    LEDColorResponse,
    LEDEffectRequest,
    LEDEffectResponse,
    LEDOffRequest,
    LEDPaintRequest,
    LEDSolidRequest,
    LEDStateResponse,
    LEDStatusRequest,
    StatusResponse,
)
from hal.presets import (
    FX_SPEAKING_WAVE,
    LST_EFFECT,
    LST_PAINT,
    LST_SOLID,
    RGB_CMD_PAINT,
    RGB_CMD_SOLID,
    STATUS_LED_PRESETS,
    VALID_LED_EFFECTS,
)
from hal.drivers.rgb.effects import run_effect as _run_effect

router = APIRouter(tags=["LED"])


def _sleep_led_locked(route: str) -> bool:
    """True while the device is asleep — sleepy owns the strip.

    app_state already gates every *internal* repaint (emotion, TTS wave, music
    wave, mic-muted, restore timer) on `_sleeping`, but the HTTP routes were
    open: an agent finishing a stale task and POSTing /led/effect or
    /led/status would light the strip on a sleeping device. Writes are dropped
    here rather than queued — sleep is a state, not a pause, so a cue that
    arrives during it is stale by the time the device wakes.

    Clearing routes (/led/off, /led/effect/stop) are deliberately NOT gated:
    they drive the strip toward dark, which is what sleep already wants."""
    if not state._sleeping:
        return False
    state.logger.info("%s skipped -- sleepy owns the strip", route)
    return True


@router.get("/led", response_model=LEDStateResponse)
def get_led_state():
    """Get LED strip info."""
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    return {"led_count": state.rgb_service.led_count}


@router.get("/led/color", response_model=LEDColorResponse)
def get_led_color():
    """Get current LED state: actual pixel color read from strip, effect, scene, brightness."""
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    effect_running = (
        state._effect_name is not None
        and state._effect_thread is not None
        and state._effect_thread.is_alive()
    )
    if effect_running and state._effect_base_color:
        r, g, b = state._effect_base_color
    else:
        raw = state.rgb_service.strip.getPixelColor(0)
        r = (raw >> 16) & 0xFF
        g = (raw >> 8) & 0xFF
        b = raw & 0xFF
    brightness = round(max(r, g, b) / 255.0, 3)
    is_on = (r, g, b) != (0, 0, 0) or effect_running
    return {
        "led_count": state.rgb_service.led_count,
        "on": is_on,
        "color": [r, g, b],
        "hex": f"#{r:02x}{g:02x}{b:02x}",
        "brightness": brightness,
        "effect": state._effect_name,
        "scene": state._active_scene,
    }


@router.post("/led/solid", response_model=StatusResponse)
def set_led_solid(req: LEDSolidRequest):
    """Fill entire LED strip with a single color."""
    if _sleep_led_locked("led/solid"):
        return {"status": "ok"}
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    color = tuple(req.color) if isinstance(req.color, list) else req.color
    state._stop_current_effect()
    state.rgb_service.dispatch(RGB_CMD_SOLID, color)
    # Transient = temporary overlay that gets restored — it must not exit the
    # active scene (the boot-time breathing effect was silently killing the
    # scene re-activated after a HAL restart).
    if not req.transient:
        state._active_scene = None
    if state.sensing_service and isinstance(color, tuple):
        state.sensing_service.presence.set_last_color(color)
    if req.transient:
        state._cancel_pending_restore()
    else:
        # Explicit user look — wins over the mic-muted resting indicator.
        state._dismiss_mic_muted_led("led/solid")
        state._save_user_led_state({"type": LST_SOLID, "color": list(color)})
    return {"status": "ok"}


def _expand_gradient(stops, n: int) -> list:
    """Linearly interpolate color stops across n pixels (CSS-gradient style)."""
    rgb = []
    for c in stops:
        if isinstance(c, int):
            rgb.append(((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF))
        elif isinstance(c, (list, tuple)) and len(c) >= 3:
            rgb.append(tuple(c[:3]))
    if not rgb:
        return []
    if len(rgb) == 1 or n <= 1:
        return [rgb[0]] * max(n, 1)
    out = []
    segs = len(rgb) - 1
    for i in range(n):
        pos = i * segs / (n - 1)
        k = min(int(pos), segs - 1)
        t = pos - k
        a, b = rgb[k], rgb[k + 1]
        out.append(tuple(int(x + (y - x) * t) for x, y in zip(a, b)))
    return out


@router.post("/led/paint", response_model=StatusResponse)
def set_led_paint(req: LEDPaintRequest):
    """Set individual pixel colors (multi-color fills, gradients)."""
    if _sleep_led_locked("led/paint"):
        return {"status": "ok"}
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    if req.gradient:
        colors = _expand_gradient(req.colors, state.rgb_service.led_count)
    else:
        colors = [tuple(c) if isinstance(c, list) else c for c in req.colors]
    # A running effect repaints the strip every ~40ms and would overwrite
    # the painted pixels — stop it first, same as /led/solid.
    state._stop_current_effect()
    state.rgb_service.dispatch(RGB_CMD_PAINT, colors)
    if not req.transient:
        state._active_scene = None
    if state.sensing_service:
        avg = state._avg_paint_color(colors)
        if avg:
            state.sensing_service.presence.set_last_color(avg)
    if req.transient:
        state._cancel_pending_restore()
    else:
        state._dismiss_mic_muted_led("led/paint")
        # Persist the final pixel list (gradient already expanded) so restore
        # repaints exactly what the strip showed.
        state._save_user_led_state(
            {
                "type": LST_PAINT,
                "colors": [list(c) if isinstance(c, tuple) else c for c in colors],
            }
        )
    return {"status": "ok"}


@router.post("/led/off", response_model=StatusResponse)
def turn_off_leds(req: Optional[LEDOffRequest] = Body(default=None)):
    """Turn off all LEDs — i.e. drop the user's colour and return to the
    default resting state.

    "Off" is not a separate mode. The resting look is already dark
    (AMBIENT_RESTING_LED), so clearing the user state IS off: ambient, the
    resting settle, presence and the speaking waves all read
    led_should_stay_dark() and leave the strip alone. Actions may still light
    it briefly (an emotion) or for as long as they last (a status cue, the
    mic-muted indicator) — those are information, not decoration.

    Modelling off as its own sticky state was worse: it looked identical to
    the default (both dark) but behaved differently, and nothing could return
    the device to the default — an explicit colour was the only way out, so
    the user could never get back to "dark at rest, expressive on action".
    """
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    transient = req.transient if req else False
    state._stop_current_effect()
    state.rgb_service.clear()
    if not transient:
        state._active_scene = None
    if state.sensing_service:
        state.sensing_service.presence.set_last_color((0, 0, 0))
    if transient:
        state._cancel_pending_restore()
    else:
        state._dismiss_mic_muted_led("led/off")
        state._save_user_led_state(None)
    return {"status": "ok"}


@router.post("/led/effect", response_model=LEDEffectResponse)
def start_led_effect(req: LEDEffectRequest):
    """Start a LED effect (breathing, candle, rainbow, notification_flash, pulse)."""
    if _sleep_led_locked(f"LED effect '{req.effect}'"):
        return {"status": "ok", "effect": req.effect, "speed": req.speed}
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    if req.effect not in VALID_LED_EFFECTS:
        raise HTTPException(
            400, f"Unknown effect '{req.effect}'. Available: {VALID_LED_EFFECTS}"
        )

    if state._tts_speaking and req.effect != FX_SPEAKING_WAVE:
        state.logger.info("LED effect '%s' skipped -- TTS speaking_wave active", req.effect)
        return {"status": "ok", "effect": req.effect, "speed": req.speed}

    # Mic-muted red is a privacy indicator — a transient system overlay
    # (ambient breathing, Buddy Busy pulse, statusled) must NOT paint over
    # it. Ambient's breathingLoop reads the current color and re-fires every
    # 2s (see system/ambient/service.go), and without this guard each tick
    # would kill our red thread and start ambient's breathing in some dim
    # frame we happened to sample. User-initiated writes (non-transient) go
    # through unchanged and dismiss mic-muted below via _dismiss_mic_muted_led.
    if req.transient and state._mic_muted_led_owns_strip():
        state.logger.info(
            "LED effect '%s' (transient) skipped -- mic-muted indicator owns strip",
            req.effect,
        )
        return {"status": "ok", "effect": req.effect, "speed": req.speed}

    # NOTE: no "light is off" guard here. A transient effect on this route is
    # a status cue (connectivity, error, OTA) or a companion overlay — it
    # carries information, so it may light a resting strip. What must NOT
    # relight it is ambient breathing, and that is handled at the source
    # (system/ambient/service.go never paints a dark strip) rather than by
    # second-guessing every caller here.
    state._stop_current_effect()
    if not req.transient:
        state._active_scene = None

    base_color = tuple(req.color) if req.color else (255, 180, 100)
    # Transient effects (e.g. Buddy's Busy pulse) overlay on the user's
    # saved LED color so "đèn xanh lá" stays visible underneath the wave.
    # Non-transient effects replace the strip outright.
    overlay_base = state._get_user_base_color() if req.transient else (0, 0, 0)

    state._effect_stop.clear()
    state._effect_name = req.effect
    state._effect_base_color = base_color
    state._effect_thread = threading.Thread(
        target=_run_effect,
        args=(
            req.effect,
            base_color,
            req.speed,
            req.duration_ms,
            state._effect_stop,
            state.rgb_service,
        ),
        kwargs={"base_color": overlay_base, "brightness": req.brightness},
        daemon=True,
        name=f"led-effect-{req.effect}",
    )
    state._effect_thread.start()
    state.logger.info(
        "LED effect started: %s (speed=%.1f, duration=%s, transient=%s)",
        req.effect,
        req.speed,
        req.duration_ms,
        req.transient,
    )

    if req.transient:
        state._cancel_pending_restore()
    else:
        state._dismiss_mic_muted_led("led/effect")
        state._save_user_led_state(
            {
                "type": LST_EFFECT,
                "effect": req.effect,
                "color": list(base_color),
                "speed": req.speed,
                "brightness": req.brightness,
            }
        )

    return {"status": "ok", "effect": req.effect, "speed": req.speed}


@router.post("/led/status", response_model=LEDEffectResponse)
def set_led_status(req: LEDStatusRequest):
    """Apply an os-server status state (booting/error/ota/…) by NAME. The os-server
    owns the status state machine (WHEN); HAL owns the appearance (WHAT) via
    STATUS_LED_PRESETS, overridable per device in presets.json. Applied transiently
    through the normal effect path, so it never clobbers the user's saved LED state."""
    preset = STATUS_LED_PRESETS.get(req.state)
    if not preset:
        raise HTTPException(
            400,
            f"Unknown status state '{req.state}'. Available: {sorted(STATUS_LED_PRESETS)}",
        )
    effect, color, speed = preset["effect"], preset["color"], preset.get("speed", 1.0)
    # "solid" is a persistent fill (e.g. the setup-ready white): it is the
    # displayed state, so it saves user LED state like /led/solid. Every other
    # status is a transient effect overlay that never clobbers user state.
    if effect == "solid":
        set_led_solid(LEDSolidRequest(color=color))
    else:
        start_led_effect(LEDEffectRequest(effect=effect, color=color, speed=speed, transient=True))
    return {"status": "ok", "effect": effect, "speed": speed}


@router.post("/led/restore", response_model=StatusResponse)
def restore_led():
    """Restore the strip to the user's saved LED state.

    Used by Buddy (and other transient drivers) after they release the
    strip. If no user state exists, the strip is cleared to off so the
    transient color/effect doesn't linger.
    """
    if _sleep_led_locked("led/restore"):
        return {"status": "ok"}
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    if state._tts_speaking:
        state.logger.info("LED restore skipped -- TTS speaking_wave active")
        return {"status": "ok"}
    if state._mic_muted_led_owns_strip():
        # A transient driver releasing the strip while muted settles on the
        # privacy indicator (the no-user-state branch below would clear it).
        state._start_mic_muted_effect()
        state.logger.info("LED restore: mic muted -- settling on privacy indicator")
        return {"status": "ok"}
    user_state = state._user_led_state
    if user_state is None:
        # No saved user preference — settle on the ambient resting look
        # (mirrors the Go ambient loop fallback) so a transient overlay
        # releasing the strip (voice_service noise-session cleanup, Buddy)
        # doesn't leave the lamp in a look nobody chose for ~60s until
        # ambient.breathingLoop resumes after its interaction quiet-window.
        # Same pattern _clear_mic_muted_led uses when no user state exists.
        # A dark resting look means clear the strip — see AMBIENT_RESTING_LED.
        from hal.presets import AMBIENT_RESTING_LED, ambient_resting_is_dark

        if ambient_resting_is_dark():
            state._stop_current_effect()
            state.rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))
            state.logger.info("LED restore: no user state -- resting dark, cleared")
            return {"status": "ok"}
        state._start_preset_effect(AMBIENT_RESTING_LED, "led-ambient-fallback")
        state.logger.info("LED restore: no user state -- settling on ambient resting")
        return {"status": "ok"}
    state._restore_user_led()
    return {"status": "ok"}


@router.post("/led/effect/stop", response_model=StatusResponse)
def stop_led_effect():
    """Stop the currently running LED effect."""
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    if state._tts_speaking:
        state.logger.info("LED effect/stop skipped -- TTS speaking_wave active")
        return {"status": "ok"}
    # Same reasoning as /led/effect: don't let a transient overlay's cleanup
    # pull the mic-muted red down. While the indicator owns the strip no
    # transient overlay can be RUNNING (its start was skipped above), so any
    # stop arriving here is a stale caller: ambient's breathingLoop tracks
    # "running" locally and still calls StopEffect on pause/lock even though
    # its start was skipped. That stop used to pass when an EMOTION effect
    # held the strip (e.g. thinking's purple pulse) and killed it after ~one
    # cycle, freezing the strip on the last ripple frame — ambient's
    # follow-up breathing is also skipped while muted, so nothing repainted.
    # Emotion effects settle back onto the red via their scheduled restore.
    if state._mic_muted_led_owns_strip():
        state.logger.info("LED effect/stop skipped -- mic-muted indicator owns strip")
        return {"status": "ok"}
    state._stop_current_effect()
    # Stopping a thread does not unpaint what it drew: the strip holds the
    # effect's last frame. When the resting state is dark that remnant is the
    # visible result, so clear it. With a lit resting look the old behaviour is
    # right — the caller's follow-up restore repaints, and blanking here would
    # add a flicker in between.
    if state.led_should_stay_dark():
        state.rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))
        state.logger.info("LED effect/stop -- resting dark, cleared last frame")
    return {"status": "ok"}
