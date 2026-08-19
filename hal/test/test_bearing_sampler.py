"""Passive bearing sampling.

The rules here are all about not teaching the lamp something false: a far
stranger, a bearing computed from too large an offset, or a posture that is not
actually looking at anyone.
"""

from unittest import mock

import numpy as np
import pytest

import hal.app_state as state
import hal.config as cfg
from hal.drivers.tracking import aim, bearing_sampler


def _frame(w=1280, h=720):
    return np.zeros((h, w, 3), dtype=np.uint8)


class _Svc:
    _tracking_active = False

    def __init__(self):
        self.get_positions = mock.Mock(return_value={
            "base_yaw.pos": 10.0, "base_pitch.pos": 5.0, "elbow_pitch.pos": 20.0,
        })


class _Cap:
    def acquire_consumer(self): pass
    def release_consumer(self): pass
    last_frame_ts = 1e9

    def __init__(self, frame):
        self.last_frame = frame


def _run(box, svc=None, tracking=False, disabled=False):
    """Returns (recorded_or_None, svc). `box` is (x, y, w, h) or None."""
    svc = svc or _Svc()
    svc._tracking_active = tracking
    det = mock.Mock()
    det.detect = mock.Mock(side_effect=lambda f, t, strict=True: box if t == "person" else None)
    recorded = {}

    def _record(bearing, pose=None, now=None):
        recorded["bearing"] = bearing
        recorded["pose"] = pose
        return True

    with (
        mock.patch.object(state, "camera_capture", _Cap(_frame())),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "_camera_disabled", disabled, create=True),
        mock.patch.object(aim, "get_detector", return_value=det),
        mock.patch("hal.drivers.tracking.user_bearing.record_sighting", _record),
    ):
        ok = bearing_sampler._sample_once()
    return (recorded if ok else None), svc


def test_a_close_centred_person_is_recorded_with_posture():
    # 300px tall in a 720px frame, centred both ways.
    rec, _ = _run((490, 210, 300, 300))
    assert rec is not None
    assert rec["pose"] is not None, "a centred subject should teach the posture"
    assert rec["pose"]["base_pitch.pos"] == 5.0


def test_a_far_person_is_ignored():
    """The ~65px colleague across the office — a real person, not our user."""
    rec, _ = _run((600, 300, 60, 65))
    assert rec is None


def test_bearing_is_corrected_for_a_horizontal_offset():
    """Off-centre is fine for direction: bearing = yaw + dx x scale."""
    rec, _ = _run((740, 210, 300, 300))  # centre x=890 of 1280 -> dx=+19.5%
    assert rec is not None
    assert rec["bearing"] > 10.0, rec  # yaw was 10.0, subject is to the right


def test_a_large_horizontal_offset_is_not_recorded():
    """That correction leans on the FOV constant, so it is only trusted small."""
    rec, _ = _run((1000, 210, 280, 300))
    assert rec is None


def test_posture_is_withheld_when_the_subject_is_not_vertically_centred():
    """Pitch cannot be corrected arithmetically — storing it here would teach a
    posture aimed above or below the user."""
    rec, _ = _run((490, 0, 300, 300))  # high in frame
    assert rec is not None, "the bearing is still usable"
    assert rec["pose"] is None, "but the posture is not"


def test_nothing_is_sampled_while_the_body_is_busy():
    """Mid-aim the pose is in flight and the detector is in use."""
    rec, _ = _run((490, 210, 300, 300), tracking=True)
    assert rec is None


def test_nothing_is_sampled_when_the_camera_is_disabled():
    """Privacy: never watch someone who asked us not to."""
    rec, _ = _run((490, 210, 300, 300), disabled=True)
    assert rec is None


def test_a_look_in_progress_is_never_made_to_wait():
    """The sampler takes the detector lock non-blocking — a background learner
    must not add latency to a user's question."""
    aim._detector_lock_use.acquire()
    try:
        rec, _ = _run((490, 210, 300, 300))
    finally:
        aim._detector_lock_use.release()
    assert rec is None
