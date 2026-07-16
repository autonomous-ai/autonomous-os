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
    LST_OFF,
    LST_SOLID,
    RGB_CMD_PAINT,
    RGB_CMD_SOLID,
    STATUS_LED_PRESETS,
    VALID_LED_EFFECTS,
)
from hal.drivers.rgb.effects import run_effect as _run_effect

router = APIRouter(tags=["LED"])


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


@router.post("/led/paint", response_model=StatusResponse)
def set_led_paint(req: LEDPaintRequest):
    """Set individual pixel colors."""
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    state._dismiss_mic_muted_led("led/paint")
    colors = [tuple(c) if isinstance(c, list) else c for c in req.colors]
    state.rgb_service.dispatch(RGB_CMD_PAINT, colors)
    return {"status": "ok"}


@router.post("/led/off", response_model=StatusResponse)
def turn_off_leds(req: Optional[LEDOffRequest] = Body(default=None)):
    """Turn off all LEDs."""
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
        state._save_user_led_state({"type": LST_OFF})
    return {"status": "ok"}


@router.post("/led/effect", response_model=LEDEffectResponse)
def start_led_effect(req: LEDEffectRequest):
    """Start a LED effect (breathing, candle, rainbow, notification_flash, pulse)."""
    if not state.rgb_service:
        raise HTTPException(503, "LED not available")
    if req.effect not in VALID_LED_EFFECTS:
        raise HTTPException(
            400, f"Unknown effect '{req.effect}'. Available: {VALID_LED_EFFECTS}"
        )

    if state._tts_speaking and req.effect != FX_SPEAKING_WAVE:
        state.logger.info("LED effect '%s' skipped -- TTS speaking_wave active", req.effect)
        return {"status": "ok", "effect": req.effect, "speed": req.speed}

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
        kwargs={"base_color": overlay_base},
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
    if user_state is None or user_state.get("type") == LST_OFF:
        state._stop_current_effect()
        state.rgb_service.dispatch(RGB_CMD_SOLID, (0, 0, 0))
        state.logger.info("LED restore: no user state -- strip cleared")
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
    state._stop_current_effect()
    return {"status": "ok"}
