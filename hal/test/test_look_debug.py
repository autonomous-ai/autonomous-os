"""Focused tests for LOOK-DEBUG tracing.

The guarantee that matters most: tracing must never cost the user an answer.
Every entry point is called on the live look path, so a bug here would break
visual questions rather than just lose a trace.
"""

import json
import os
import tempfile
from unittest import mock

from hal.drivers.tracking import look_debug as ld


def _fresh(tmp, enabled="true"):
    """Reset the module's one-time config resolution."""
    ld._enabled = None
    ld._base = None
    ld._current = None
    return mock.patch.dict(os.environ, {"HAL_LOOK_DEBUG": enabled, "HAL_LOOK_DEBUG_DIR": tmp})


class _Aim:
    aimed, reason, iterations = True, "centred on person", 2
    yaw_moved_deg, final_dx_frac, bearing_steps = 7.5, 0.01, 0


def test_a_full_look_is_written_with_question_and_answer():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        ld.start()
        ld.note_aim(_Aim())
        ld.note_capture(None)
        ld.finish("OK_realtime_handled", question="what is this?", answer="a mug")
        dirs = os.listdir(tmp)
        assert len(dirs) == 1 and dirs[0].endswith("OK_realtime_handled")
        with open(os.path.join(tmp, dirs[0], "result.json"), encoding="utf-8") as f:
            r = json.load(f)
    assert r["question"] == "what is this?"
    assert r["answer"] == "a mug"
    assert r["aim"]["reason"] == "centred on person"
    assert r["aim"]["iterations"] == 2


def test_failures_are_visible_from_the_directory_name():
    # The point of putting status in the name: `ls` shows what went wrong.
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        ld.start()
        ld.abandon("no_camera_frame")
        assert any("FAIL-no_camera_frame" in d for d in os.listdir(tmp))


def test_the_captured_frame_is_copied_into_the_trace():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        src = os.path.join(tmp, "src.jpg")
        with open(src, "wb") as f:
            f.write(b"jpegbytes")
        ld.start()
        ld.note_capture(src)
        ld.finish("OK_realtime_handled")
        d = [x for x in os.listdir(tmp) if x.endswith("OK_realtime_handled")][0]
        assert os.path.exists(os.path.join(tmp, d, "capture.jpg"))


def test_disabled_by_default_and_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp, enabled="false"):
        ld.start()
        ld.note_aim(_Aim())
        ld.finish("OK_realtime_handled", answer="x")
        assert os.listdir(tmp) == []


def test_finishing_without_a_look_is_a_no_op():
    # Ordinary turns call finish() too; it must not create stray dirs.
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        ld.finish("OK_realtime_handled", answer="no look happened")
        assert os.listdir(tmp) == []


def test_tracing_never_raises_on_bad_input():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        ld.start()
        ld.note_aim(object())          # missing every attribute
        ld.note_capture("/nope.jpg")   # missing file
        ld.finish("OK_realtime_handled")  # must still write


def test_old_traces_are_pruned():
    with tempfile.TemporaryDirectory() as tmp, _fresh(tmp):
        with mock.patch.dict(os.environ, {"HAL_LOOK_DEBUG_MAX_ENTRIES": "3"}):
            for i in range(7):
                ld.start()
                ld.finish(f"OK_{i}")
        assert len(os.listdir(tmp)) <= 3
