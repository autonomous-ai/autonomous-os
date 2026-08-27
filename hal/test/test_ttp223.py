"""TTP223 session / decision state machine — the D3 gap in the build plan.

Until now the touch path had no test at all: `test_board.py` pins the wiring and
nothing covered the state machine that turns edges into gestures. Phase 3 adds
traversal classification and a second wake entrance to that machine, so the
coverage stops being optional.

Everything here drives `_on_edge(chip, gpio, level, tick)` directly with
synthetic edge sequences — the same signature lgpio calls — so no hardware, no
lgpio, and no timers wait in real time: the session and decision timers are
fired by hand.

Pads rest HIGH (pull-up), so level 0 is a touch and level 1 a release.
"""

import importlib
import os
import unittest
from unittest import mock


def _driver(swipe: bool, min_gap_ms: str = "40"):
    """Re-import the driver with the flag in a given state — SWIPE_ENABLED is
    resolved at import, which is what makes `flag off == today's behaviour`
    testable at all."""
    os.environ["HAL_TOUCH_SWIPE"] = "true" if swipe else "false"
    os.environ["HAL_TOUCH_SWIPE_MIN_GAP_MS"] = min_gap_ms
    os.environ["HAL_TOUCH_DEBUG"] = "false"
    import hal.drivers.ttp223 as t

    importlib.reload(t)
    return t


class _Harness:
    """A handler with its lines claimed and its actions replaced by recorders."""

    def __init__(self, mod, lines=(96, 98, 100), axis=None):
        self.mod = mod
        self.h = mod.TTP223Handler()
        self.h._chip, self.h._lines, self.h._axis = 0, list(lines), axis
        self.h._ignore_edges_until = 0.0  # settle window already elapsed
        self.fired = []

    def touch(self, line, at_ms=None):
        """One touch edge, optionally at a controlled monotonic time."""
        if at_ms is None:
            self.h._on_edge(0, line, 0, 0)
            return
        with mock.patch.object(self.mod.time, "monotonic", return_value=at_ms / 1000.0):
            self.h._on_edge(0, line, 0, 0)

    def release(self, line):
        self.h._on_edge(0, line, 1, 0)

    def end_session(self):
        """Fire the session-end timer by hand."""
        if self.h._session_end_timer is not None:
            self.h._session_end_timer.cancel()
            self.h._session_end_timer = None
        self.h._on_session_end()

    def decide(self):
        if self.h._decision_timer is not None:
            self.h._decision_timer.cancel()
            self.h._decision_timer = None
        self.h._on_decision()


def _record(mod, harness):
    """Patch every action the driver can dispatch."""
    names = [
        "single_click_action",
        "head_pat_action",
        "swipe_action",
        "mic_toggle_action",
    ]
    patches = [
        mock.patch.object(
            mod, n, side_effect=lambda *a, _n=n, **k: harness.fired.append(_n)
        )
        for n in names
    ]
    # _ack_first_session does I/O on a thread; silence it.
    patches.append(mock.patch.object(harness.h, "_ack_first_session", lambda: None))
    return patches


class _Base(unittest.TestCase):
    swipe = False
    min_gap = "40"

    def setUp(self):
        self.mod = _driver(self.swipe, self.min_gap)
        self.hz = _Harness(self.mod)
        self._p = _record(self.mod, self.hz)
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])


class TestFlagOffIsUnchanged(_Base):
    """The default path must behave exactly as the shipped driver always has."""

    swipe = False

    def test_one_contact_resolves_to_tap(self):
        self.hz.touch(96)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["single_click_action"])

    def test_two_contacts_fire_pet_inline_on_count(self):
        for _ in range(2):
            self.hz.touch(96)
            self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_monotonic_three_pad_traversal_is_still_only_a_tap(self):
        """With the flag off, traversal is recorded but must not classify —
        otherwise 'default is unchanged' is not true."""
        for i, line in enumerate((96, 98, 100)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["single_click_action"])

    def test_pet_cooldown_swallows_further_contacts(self):
        for _ in range(2):
            self.hz.touch(96)
            self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])
        self.hz.touch(96)
        self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])  # nothing new

    def test_settle_window_suppresses_edges_entirely(self):
        self.hz.h._ignore_edges_until = self.mod.time.monotonic() + 5
        self.hz.touch(96)
        self.assertIsNone(self.hz.h._session_end_timer)
        self.assertEqual(self.hz.h._pad_seq, [])


class TestSwipe(_Base):
    swipe = True

    def test_monotonic_pass_over_all_pads_is_a_swipe(self):
        for i, line in enumerate((96, 98, 100)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["swipe_action"])

    def test_a_swipe_never_also_fires_a_tap(self):
        for i, line in enumerate((96, 98, 100)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertNotIn("single_click_action", self.hz.fired)

    def test_reverse_direction_is_also_a_swipe(self):
        """Direction is deliberately not used to select the action."""
        for i, line in enumerate((100, 98, 96)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["swipe_action"])

    def test_cross_talk_burst_is_not_a_swipe(self):
        """Device-measured shape: three pads inside ~20ms from one finger.
        Ordering alone is not evidence — the gaps have to clear the floor."""
        for i, line in enumerate((96, 98, 100)):
            self.hz.touch(line, at_ms=i * 10)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["single_click_action"])

    def test_two_pads_is_not_a_swipe(self):
        for i, line in enumerate((96, 98)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["single_click_action"])


class TestPetKeepsWorking(_Base):
    swipe = True

    def test_turning_around_fires_pet_immediately(self):
        """Reversal cannot later become a swipe, so pet keeps its fast path
        rather than waiting out the decision window."""
        for i, line in enumerate((96, 98, 100, 98, 96)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_stroke_reading_as_ONE_pad_becomes_a_double_tap_not_a_pet(self):
        """The accepted cost of giving double tap an action.

        "Two contacts, same pad" is simultaneously the double-tap signature and
        the pet count-fallback signature. They are indistinguishable at the
        signal level, so only one can win, and double tap does (decided
        2026-08-27). The consequence, pinned here so it is impossible to change
        by accident: a stroke so noisy that it registers as a single pad now
        MUTES THE MIC instead of giggling.

        The pet fallback still covers the commoner failure — a stroke whose pads
        move but never resolve a clean reversal — see the test below.
        """
        for _ in range(2):
            self.hz.touch(96)
            self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["mic_toggle_action"])

    def test_multi_pad_contacts_without_reversal_still_fall_back_to_pet(self):
        for n, line in enumerate((96, 98)):
            self.hz.touch(line, at_ms=n * 100)
            self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["head_pat_action"])


class TestDoubleTap(_Base):
    swipe = True

    def test_repeated_contact_on_one_pad_toggles_the_mic(self):
        for _ in range(2):
            self.hz.touch(96)
            self.hz.end_session()
        self.hz.decide()
        # Same pad twice, no traversal: the case ttp223.py:70-76 documented as a
        # known false positive now resolves to its own gesture.
        self.assertEqual(self.hz.fired, ["mic_toggle_action"])

    def test_a_single_contact_is_not_a_double_tap(self):
        self.hz.touch(96)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["single_click_action"])


class TestSequenceHygiene(_Base):
    swipe = True

    def test_a_pad_refiring_under_a_still_finger_is_not_a_step(self):
        """FastMode re-triggers on a stationary finger; counting the repeat
        would invent a direction change."""
        for line in (96, 96, 96, 98, 100):
            self.hz.touch(line)
        self.assertEqual([l for l, _ in self.hz.h._pad_seq], [96, 98, 100])

    def test_release_edges_never_enter_the_sequence(self):
        self.hz.touch(96)
        self.hz.release(96)
        self.hz.touch(98)
        self.assertEqual([l for l, _ in self.hz.h._pad_seq], [96, 98])

    def test_sequence_is_cleared_once_a_gesture_resolves(self):
        """A stale sequence would leak the previous gesture's traversal into
        the next one and misclassify it."""
        for i, line in enumerate((96, 98, 100)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.h._pad_seq, [])


class TestAxis(_Base):
    swipe = True

    def test_absent_axis_falls_back_to_line_order(self):
        """`axis` is unmeasured until Phase 2.2. A reversal is a reversal
        whichever way the pads are numbered; only direction would be wrong, and
        the driver deliberately does not use direction."""
        self.assertIsNone(self.hz.h._axis)
        for i, line in enumerate((96, 98, 100)):
            self.hz.touch(line, at_ms=i * 100)
        _, reversals, monotonic, _ = self.hz.h._traversal()
        self.assertEqual(reversals, 0)
        self.assertTrue(monotonic)

    def test_a_measured_axis_reorders_the_reversal_test(self):
        """With a real axis where line order != spatial order, the same edge
        sequence reads as a turn rather than a straight pass."""
        hz = _Harness(self.mod, axis=[96, 100, 98])
        for i, line in enumerate((96, 98, 100)):
            hz.touch(line, at_ms=i * 100)
        _, reversals, monotonic, _ = hz.h._traversal()
        self.assertEqual(reversals, 1)
        self.assertFalse(monotonic)


if __name__ == "__main__":
    unittest.main()
