"""Emotion route handler -- /emotion endpoint."""

import threading

from fastapi import APIRouter, HTTPException

import hal.app_state as state
from hal.models import EmotionRequest, EmotionResponse
from hal.presets import (
    EMOTION_PRESETS,
    EMO_CARING,
    EMO_CONFUSED,
    EMO_CURIOUS,
    EMO_EXCITED,
    EMO_GREETING,
    EMO_HAPPY,
    EMO_IDLE,
    EMO_LAUGH,
    EMO_LISTENING,
    EMO_SAD,
    EMO_SHOCK,
    EMO_SHY,
    EMO_SLEEPY,
    EMO_STRETCHING,
    LST_OFF,
    SERVO_CMD_PLAY,
)

# Emotions that can wake the device from sleep.
# greeting/stretching/sleepy = direct wake triggers.
# happy/excited/caring/laugh/curious/sad/shy/shock/confused = agent responding to user interaction.
# thinking/idle/acknowledge/nod/headshake/scan/music_* do NOT wake (background processing).
_WAKE_EMOTIONS = {
    EMO_GREETING, EMO_STRETCHING, EMO_SLEEPY,
    EMO_HAPPY, EMO_EXCITED, EMO_CARING, EMO_LAUGH, EMO_CURIOUS,
    EMO_SAD, EMO_SHY, EMO_SHOCK, EMO_CONFUSED,
}

# Auto-release the servo after this many seconds of *continuous* sleepy.
# The device is presumed unattended at that point; releasing prevents servo
# heat / wear during long idle periods.
SLEEPY_AUTO_RELEASE_SECONDS = 15 * 60

router = APIRouter(tags=["Emotion"])


@router.get("/emotion/status")
def emotion_status():
    """Return current emotion state."""
    return {
        "current_emotion": state._current_emotion,
        "sleeping": state._sleeping,
        "active_scene": state._active_scene,
    }


@router.get("/emotion/presets")
def list_emotion_presets():
    """Return all available emotion presets with their LED color and effect."""
    result = {}
    for name, preset in EMOTION_PRESETS.items():
        result[name] = {
            "color": preset.get("color"),
            "effect": preset.get("effect"),
            "speed": preset.get("speed"),
        }
    return result


@router.post("/emotion", response_model=EmotionResponse)
def express_emotion(req: EmotionRequest):
    """Express an emotion by coordinating servo animation + LED color simultaneously."""
    preset = EMOTION_PRESETS.get(req.emotion)
    if not preset:
        available = list(EMOTION_PRESETS.keys())
        raise HTTPException(
            400, f"Unknown emotion '{req.emotion}'. Available: {available}"
        )

    state.logger.info("POST /emotion: emotion=%s intensity=%s user_state=%s sleeping=%s",
                       req.emotion, req.intensity,
                       state._user_led_state.get("type") if state._user_led_state else None,
                       state._sleeping)

    if state._sleeping and req.emotion not in _WAKE_EMOTIONS:
        state.logger.info("POST /emotion: ignored %s while sleeping", req.emotion)
        return {"status": "ignored", "emotion": req.emotion, "servo": None, "led": None}

    was_sleeping = state._sleeping
    state._sleeping = req.emotion == EMO_SLEEPY
    state._current_emotion = req.emotion

    # Sleepy auto-release: fires only if sleepy stays continuous for the
    # full window. Any other emotion (including a wake) cancels the timer.
    if state._sleepy_release_timer is not None:
        state._sleepy_release_timer.cancel()
        state._sleepy_release_timer = None
    if req.emotion == EMO_SLEEPY:
        def _auto_release_after_sleepy():
            # Re-check inside the timer callback in case the state changed
            # between the cancel-window and the timer firing.
            if state._current_emotion != EMO_SLEEPY:
                return
            try:
                from hal.routes.servo import release_servos

                state.logger.info(
                    "Auto-release: sleepy held >= %ds, releasing servo",
                    SLEEPY_AUTO_RELEASE_SECONDS,
                )
                release_servos()
            except Exception as e:
                state.logger.warning(f"Sleepy auto-release failed: {e}")

        state._sleepy_release_timer = threading.Timer(
            SLEEPY_AUTO_RELEASE_SECONDS, _auto_release_after_sleepy
        )
        state._sleepy_release_timer.daemon = True
        state._sleepy_release_timer.start()

    # Auto-off scene when waking from sleep (e.g. Night mode → restore peripherals)
    if was_sleeping and not state._sleeping and state._active_scene:
        from hal.routes.scene import deactivate_scene
        state.logger.info("POST /emotion: waking from sleep, auto scene off (%s)", state._active_scene)
        deactivate_scene()

    # Two levels of suppression:
    #   - hold_mode: /servo/hold or focus/reading scene. Suppress most
    #     emotion servo animations. Scene-preset holds let scene-change
    #     emotions through (greeting/sleepy/stretching may legitimately
    #     transition scene) — but an EXPLICIT /servo/hold (_hold_explicit,
    #     agent command like "face the wall and stay there") blocks those
    #     too: a trailing [HW:/emotion:greeting] in the same reply used to
    #     ride the exemption and park the arm at the greeting pose instead
    #     of the commanded one.
    #   - tracking_active: vision tracker owns the servo. Suppress ALL
    #     emotion servo including scene-change — otherwise a loud-noise
    #     shock reaction would yank the device off the tracked object.
    # LED display updates in both cases so the user still gets visual
    # feedback.
    svc = state.animation_service
    tracking_active = svc and getattr(svc, "_tracking_active", False)
    servo_held = svc and getattr(svc, "_hold_mode", False)
    hold_explicit = svc and getattr(svc, "_hold_explicit", False)
    scene_change = req.emotion in {EMO_GREETING, EMO_SLEEPY, EMO_STRETCHING}
    servo_blocked = tracking_active or (servo_held and (hold_explicit or not scene_change))

    servo_played = None

    if svc and preset.get("servo") and not servo_blocked:
        try:
            # Sleepy auto-release stops the event loop and disables torque.
            # Restart the loop here so the wake animation actually plays —
            # mirrors the same restart in /servo/play.
            if not svc._running.is_set():
                svc._running.set()
                svc._event_thread = threading.Thread(
                    target=svc._event_loop, daemon=True
                )
                svc._event_thread.start()
                state.logger.info(
                    "Animation event loop restarted via /emotion (post-release wake)"
                )
            svc.dispatch(SERVO_CMD_PLAY, preset["servo"])
            servo_played = preset["servo"]
        except Exception as e:
            state.logger.warning(f"Emotion servo failed: {e}")
    elif servo_blocked:
        reason = "tracking active" if tracking_active else "hold mode"
        state.logger.info("POST /emotion: servo suppressed (%s) -- %s", req.emotion, reason)

    # LED behavior:
    #   - tracking_active: LED still updates so the user sees emotion
    #     feedback (eyes/color) even though the servo is locked.
    #   - hold_mode (non-scene-change): LED suppressed — /servo/hold asks
    #     for a fully "held" device, including ambient light.
    #   - otherwise: LED updates normally.
    led_allowed = tracking_active or not servo_blocked
    led_color = state._apply_emotion_led_display(req.emotion, req.intensity) if led_allowed else None

    if req.emotion == EMO_IDLE:
        pass
    elif req.emotion == EMO_SLEEPY:
        pass
    elif req.emotion == EMO_LISTENING:
        # Hold blue-pulse for the whole STT session — next emotion (LLM
        # response or voice_service idle-reset safety net) overwrites it.
        pass
    elif req.emotion == EMO_SHOCK:
        state._schedule_led_restore(2.0)
        state.logger.info("Emotion: shock -- LED restore scheduled in 2.0s")
    else:
        servo_name = preset.get("servo", "")
        restore_delay = state._get_recording_duration(servo_name) + 0.5 if servo_name else 3.5
        state.logger.info("Emotion: %s -- LED restore scheduled in %.1fs (servo=%s)", req.emotion, restore_delay, servo_name)
        state._schedule_led_restore(restore_delay)

    cam = preset.get("camera")
    if cam == LST_OFF:
        state._auto_camera_off(f"emotion:{req.emotion}")
    elif cam == "on" and state._camera_disabled:
        state._auto_camera_on(f"emotion:{req.emotion}")

    return {
        "status": "ok",
        "emotion": req.emotion,
        "servo": servo_played,
        "led": led_color,
    }
