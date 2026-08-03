"""Reachy music groove repeats until music_stop (hal/drivers/motors/reachy_service.py).

The Pollen SDK's play_move() runs a recorded move exactly once, so without the
repeat in _play_recording the robot danced a few seconds and then sat still for
the rest of the track — where the feetech backend loops the groove
(animation_service._continue_playback).

The reachy_mini SDK is not installed on dev machines, so it is stubbed here;
numpy/scipy come from hal's venv (imported at module level by the driver).
"""
import os
import sys
import threading
import time
import types
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _install_sdk_stub():
    """Register a minimal fake `reachy_mini` package before importing the driver."""
    if "reachy_mini" in sys.modules:
        return
    pkg = types.ModuleType("reachy_mini")
    pkg.ReachyMini = object          # replaced per-test by a fake instance
    utils = types.ModuleType("reachy_mini.utils")
    utils.create_head_pose = lambda **kwargs: None
    motion = types.ModuleType("reachy_mini.motion")
    recorded = types.ModuleType("reachy_mini.motion.recorded_move")
    recorded.RecordedMoves = object
    sys.modules.update({
        "reachy_mini": pkg,
        "reachy_mini.utils": utils,
        "reachy_mini.motion": motion,
        "reachy_mini.motion.recorded_move": recorded,
    })


_install_sdk_stub()

import hal.presets as P  # noqa: E402
from hal.drivers.motors.reachy_service import ReachyMotionService  # noqa: E402

_MOVE_DURATION_S = 0.02


class FakeMini:
    """Daemon client stand-in: records every played move, honours cancel_move."""

    def __init__(self):
        self.played = []
        self._lock = threading.Lock()
        self._cancel = threading.Event()

    def play_move(self, move):
        with self._lock:
            self.played.append(move)
        # A real move blocks for its duration; cancel_move cuts it short.
        self._cancel.wait(_MOVE_DURATION_S)
        self._cancel.clear()

    def cancel_move(self):
        self._cancel.set()

    def enable_motors(self):
        pass

    def disable_motors(self):
        pass

    def wake_up(self):
        pass

    def goto_sleep(self):
        pass

    def goto_target(self, **kwargs):
        pass

    def play_count(self, name=None):
        with self._lock:
            if name is None:
                return len(self.played)
            return sum(1 for m in self.played if m == name)


class FakeMoves:
    """RecordedMoves stand-in — a move IS its HF name, so calls are inspectable.

    Unknown names raise like the real library does (it has `dance1`, not `dance`).
    """

    _KNOWN = ("dance1", "dance2", "dance3", "curious1", "cheerful1", "thoughtful1", "amazed1")

    def get(self, name):
        if name not in self._KNOWN:
            raise RuntimeError(
                f"Move {name} not found in recorded moves library "
                "pollen-robotics/reachy-mini-emotions-library"
            )
        return name

    def list_moves(self):
        return list(self._KNOWN)


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestReachyPlaybackLoop(unittest.TestCase):
    def setUp(self):
        self.svc = ReachyMotionService()
        self.mini = FakeMini()
        self.svc._mini = self.mini
        self.svc._moves = FakeMoves()   # skip the lazy HF loader

    def tearDown(self):
        self.svc._music_playing = False
        self.svc._music_recording = None
        self.svc._suppressed = True
        self.mini.cancel_move()
        thread = self.svc._play_thread
        if thread:
            thread.join(timeout=2.0)

    def test_single_play_runs_once(self):
        """A plain emotion play must NOT repeat — only music does."""
        self.svc.dispatch(P.SERVO_CMD_PLAY, P.SERVO_CURIOUS)
        self.svc._play_thread.join(timeout=2.0)
        self.assertEqual(self.mini.play_count("curious1"), 1)

    def test_music_start_repeats_until_stop(self):
        self.svc.dispatch(P.SERVO_CMD_MUSIC_START, P.SERVO_MUSIC_GROOVE)
        self.assertTrue(
            _wait_until(lambda: self.mini.play_count("dance1") >= 3),
            f"groove did not repeat: {self.mini.played}",
        )

        self.svc.dispatch(P.SERVO_CMD_MUSIC_STOP, None)
        self.svc._play_thread.join(timeout=2.0)
        settled = self.mini.play_count()
        time.sleep(_MOVE_DURATION_S * 5)
        self.assertEqual(self.mini.play_count(), settled, "groove kept playing after stop")

    def test_music_start_without_style_falls_back_to_groove(self):
        self.svc.dispatch(P.SERVO_CMD_MUSIC_START, None)
        self.assertTrue(_wait_until(lambda: self.mini.play_count("dance1") >= 1))

    def test_emotion_during_music_returns_to_groove(self):
        """Mirrors feetech: a finished one-shot hands the servo back to music."""
        self.svc.dispatch(P.SERVO_CMD_MUSIC_START, P.SERVO_MUSIC_JAZZ)   # dance2
        self.assertTrue(_wait_until(lambda: self.mini.play_count("dance2") >= 1))

        self.svc.dispatch(P.SERVO_CMD_PLAY, P.SERVO_CURIOUS)
        self.assertTrue(_wait_until(lambda: self.mini.play_count("curious1") >= 1))
        # The emotion play thread keeps grooving once its one-shot is done.
        played_after = self.mini.play_count("dance2")
        self.assertTrue(
            _wait_until(lambda: self.mini.play_count("dance2") > played_after),
            f"groove did not resume after emotion: {self.mini.played}",
        )

    def test_unknown_recording_during_music_keeps_the_groove(self):
        """The agent sends names the HF library lacks (`dance`, not `dance1`).

        That must not end the dance for the rest of the track — it did, which
        is how the robot went still mid-song on the first live run.
        """
        self.svc.dispatch(P.SERVO_CMD_MUSIC_START, P.SERVO_MUSIC_GROOVE)
        self.assertTrue(_wait_until(lambda: self.mini.play_count("dance1") >= 1))

        self.svc.dispatch(P.SERVO_CMD_PLAY, "dance")   # not in the HF library
        played_after = self.mini.play_count("dance1")
        self.assertTrue(
            _wait_until(lambda: self.mini.play_count("dance1") > played_after),
            f"groove died on an unknown recording: {self.mini.played}",
        )
        self.assertEqual(self.mini.play_count("dance"), 0, "bogus move must not play")

    def test_groove_move_that_always_fails_does_not_spin(self):
        """A broken groove must end the thread, not busy-loop on failures."""
        class DeadMoves(FakeMoves):
            def get(self, name):
                raise RuntimeError(f"Move {name} not found")

        self.svc._moves = DeadMoves()
        self.svc.dispatch(P.SERVO_CMD_MUSIC_START, P.SERVO_MUSIC_GROOVE)
        self.svc._play_thread.join(timeout=2.0)
        self.assertFalse(self.svc._play_thread.is_alive(), "play thread spun on a failing move")
        self.assertEqual(self.mini.play_count(), 0)

    def test_hold_stops_the_groove(self):
        """suppress-then-cancel ordering: hold must not be outrun by a repeat."""
        self.svc.dispatch(P.SERVO_CMD_MUSIC_START, P.SERVO_MUSIC_GROOVE)
        self.assertTrue(_wait_until(lambda: self.mini.play_count("dance1") >= 1))

        self.svc.hold()
        self.svc._play_thread.join(timeout=2.0)
        settled = self.mini.play_count()
        time.sleep(_MOVE_DURATION_S * 5)
        self.assertEqual(self.mini.play_count(), settled, "groove survived hold()")

    def test_stop_ends_the_groove(self):
        self.svc.dispatch(P.SERVO_CMD_MUSIC_START, P.SERVO_MUSIC_GROOVE)
        self.assertTrue(_wait_until(lambda: self.mini.play_count("dance1") >= 1))

        self.svc.stop(timeout=2.0)
        self.assertFalse(self.svc._music_playing)
        settled = self.mini.play_count()
        time.sleep(_MOVE_DURATION_S * 5)
        self.assertEqual(self.mini.play_count(), settled, "groove survived stop()")


class TestAvailableRecordings(unittest.TestCase):
    """GET /servo must list the same vocabulary its `current` field reports."""

    def setUp(self):
        self.svc = ReachyMotionService()
        self.svc._moves = FakeMoves()

    def test_mapped_moves_are_listed_under_their_hal_name(self):
        listed = self.svc.get_available_recordings()
        self.assertIn(P.SERVO_MUSIC_GROOVE, listed)       # not 'dance1'
        self.assertIn(P.SERVO_THINKING_DEEP, listed)      # not 'thoughtful1'
        self.assertNotIn("dance1", listed)
        self.assertNotIn("thoughtful1", listed)

    def test_unmapped_hf_moves_stay_listed_verbatim(self):
        self.assertIn("amazed1", self.svc.get_available_recordings())

    def test_current_recording_is_always_in_the_list(self):
        """What the web highlights (`current`) must exist among the items."""
        listed = set(self.svc.get_available_recordings())
        for name in (P.SERVO_MUSIC_GROOVE, P.SERVO_CURIOUS, P.SERVO_HAPPY_WIGGLE):
            self.assertIn(name, listed)

    def test_no_move_library_returns_empty(self):
        self.svc._moves = False
        self.assertEqual(self.svc.get_available_recordings(), [])


if __name__ == "__main__":
    unittest.main()
