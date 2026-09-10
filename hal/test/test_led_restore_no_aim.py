"""An LED restore repaints the strip. It must not move the body (#314).

`_restore_user_led()` is called after almost every emotion, at every TTS end, at
music end, on mic unmute, 3.0s after an STT session opens, and on a rejected
wake-word turn. While a scene was the saved LED state it spawned a
`restore-aim-<dir>` thread on each of those, and `aim_servo` honours nothing but
the sleep lock — so it killed the running recording mid-frame and parked the
head as `__aim_hold__` for 5 seconds. That read to users as "the lamp yanks its
head back when it starts listening".

Any thread started from the scene branch is the aim: the branch stops effects
and dispatches a solid colour, neither of which spawns one.
"""

from unittest import mock

import pytest

import hal.app_state as app_state
from hal.presets import LST_SCENE, LST_SOLID, RGB_CMD_SOLID, SCENE_PRESETS


@pytest.fixture
def quiet_strip(monkeypatch):
    """Neutralise every early return in _restore_user_led, and hand back the RGB double."""
    rgb = mock.Mock()
    monkeypatch.setattr(app_state, "rgb_service", rgb, raising=False)
    monkeypatch.setattr(app_state, "animation_service", mock.Mock(), raising=False)
    monkeypatch.setattr(app_state, "_sleeping", False, raising=False)
    monkeypatch.setattr(app_state, "_tts_speaking", False, raising=False)
    monkeypatch.setattr(app_state, "_music_playing", False, raising=False)
    monkeypatch.setattr(app_state, "_thinking_cue_active", False, raising=False)
    monkeypatch.setattr(app_state, "_mic_muted_led_owns_strip", lambda: False)
    monkeypatch.setattr(app_state, "_stop_current_effect", mock.Mock())
    return rgb


def _a_scene_with_an_aim():
    """The scene names come from presets; pick one that actually aims."""
    for name, preset in SCENE_PRESETS.items():
        if preset.get("aim"):
            return name, preset
    pytest.skip("no scene preset declares an aim")


def test_a_scene_restore_starts_no_thread(quiet_strip, monkeypatch):
    scene, _preset = _a_scene_with_an_aim()
    monkeypatch.setattr(
        app_state, "_user_led_state", {"type": LST_SCENE, "scene": scene}, raising=False
    )

    with mock.patch.object(app_state.threading, "Thread") as thread_cls:
        app_state._restore_user_led()

    assert thread_cls.call_args_list == [], (
        "an LED restore must not spawn a servo aim — it kills the running "
        "animation and parks the head as __aim_hold__ for 5s"
    )


def test_a_scene_restore_reaches_the_end_of_its_branch(quiet_strip, monkeypatch, caplog):
    """The repaint must happen because the branch ran, not because it threw.

    Without this, deleting the aim and crashing before it would look identical
    to test_a_scene_restore_starts_no_thread: the whole body sits in a
    `try/except Exception` that logs "LED restore failed" and swallows it.
    """
    scene, _preset = _a_scene_with_an_aim()
    monkeypatch.setattr(
        app_state, "_user_led_state", {"type": LST_SCENE, "scene": scene}, raising=False
    )

    with caplog.at_level("WARNING", logger=app_state.logger.name):
        app_state._restore_user_led()

    assert "LED restore failed" not in caplog.text, caplog.text


def test_a_scene_restore_still_repaints_the_strip(quiet_strip, monkeypatch):
    scene, preset = _a_scene_with_an_aim()
    monkeypatch.setattr(
        app_state, "_user_led_state", {"type": LST_SCENE, "scene": scene}, raising=False
    )

    app_state._restore_user_led()

    expected = tuple(int(c * preset["brightness"]) for c in preset["color"])
    quiet_strip.dispatch.assert_called_once_with(RGB_CMD_SOLID, expected)


def test_a_solid_restore_is_untouched(quiet_strip, monkeypatch):
    # The repaint path is the point of the function; proving it intact keeps
    # this from being a test that only says "nothing happens".
    monkeypatch.setattr(
        app_state, "_user_led_state", {"type": LST_SOLID, "color": [10, 20, 30]}, raising=False
    )

    app_state._restore_user_led()

    quiet_strip.dispatch.assert_called_once_with(RGB_CMD_SOLID, (10, 20, 30))
