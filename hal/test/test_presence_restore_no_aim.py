"""A presence light restore restores light. Nothing else (#314).

`_restore_light()` runs on every IDLE/AWAY -> PRESENT. It used to call back into
`aim_servo` whenever a scene was active, and `aim_servo` honours nothing but the
sleep lock — so returning to the room killed whatever recording was playing and
parked the head as `__aim_hold__` for 5 seconds.

Deliberately not in test_presence_wake_focus.py: that file imports SensingService,
which pulls AnimationService and therefore lerobot, so it only collects on a
device. presence_service.py has no such dependency and these run anywhere.
"""

import inspect
from unittest import mock

import pytest

from hal.drivers.sensing.presence_service import PresenseService
from hal.presets import RGB_CMD_SOLID


def _present_service(rgb):
    svc = PresenseService(rgb_service=rgb)
    svc._last_color = (10, 20, 30)
    # The strip is lit; the restore path is only interesting when it repaints.
    svc._light_is_off = lambda: False
    return svc


def test_a_presence_light_restore_does_not_move_the_body():
    rgb = mock.Mock()
    svc = _present_service(rgb)

    svc._restore_light()

    assert not hasattr(svc, "_on_restore_aim"), (
        "the aim callback is gone — a presence transition must not command a servo"
    )


def test_the_aim_callback_can_no_longer_be_wired_in():
    """The parameter is removed, not just left unused.

    Asserting on the signature rather than on behaviour: re-adding the callback
    has to be a deliberate act that deletes this test, not an argument that
    quietly starts working again.
    """
    assert "on_restore_aim" not in inspect.signature(PresenseService.__init__).parameters

    with pytest.raises(TypeError):
        PresenseService(rgb_service=mock.Mock(), on_restore_aim=lambda: None)


def test_the_light_still_comes_back():
    # The repaint is the whole job of _restore_light; proving it intact keeps
    # this from being a test that only says "nothing happens".
    rgb = mock.Mock()
    svc = _present_service(rgb)

    svc._restore_light()

    rgb.dispatch.assert_called_once_with(RGB_CMD_SOLID, (10, 20, 30))


def test_a_dark_strip_is_left_dark():
    # Resting-dark is a real state, not an absence of one: repainting it would
    # turn the lamp on by itself when somebody walks past.
    rgb = mock.Mock()
    svc = _present_service(rgb)
    svc._light_is_off = lambda: True

    svc._restore_light()

    assert not rgb.dispatch.called
