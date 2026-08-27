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
        self.assertEqual(self.hz.h._contact, [])


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

    def test_both_directions_are_the_same_gesture(self):
        """Left-to-right and right-to-left both resolve to swipe_action, which
        always sleeps. Direction is not read anywhere in the path."""
        for lines in ((96, 98, 100), (100, 98, 96)):
            self.hz.fired.clear()
            self.hz.h._reset_cycle()
            for i, line in enumerate(lines):
                self.hz.touch(line, at_ms=i * 100)
            self.hz.end_session()
            self.hz.decide()
            self.assertEqual(self.hz.fired, ["swipe_action"], lines)

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

    def test_a_back_and_forth_stroke_is_a_pet_not_a_swipe(self):
        """Device trace 160546: the user stroked left-right-left and got
        DOUBLE_TAP. Each leg of a stroke is itself a clean one-direction pass,
        so a swipe has to be the whole gesture — one contact — or every pet
        resolves as a swipe."""
        for lines in ((98, 100, 96), (100, 98, 96)):
            for i, line in enumerate(lines):
                self.hz.touch(line, at_ms=len(self.hz.h._contacts) * 1000 + i * 120)
            self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_a_hand_moving_between_contacts_fires_pet_immediately(self):
        """A stroke is contacts in DIFFERENT places. Once they share no pad the
        gesture cannot be a double tap, so pet keeps its fast path rather than
        waiting out the decision window."""
        for n, line in enumerate((96, 98), 1):
            self.hz.touch(line)
            self.hz.end_session()
        self.assertEqual(self.hz.fired, ["head_pat_action"])

    def test_one_contact_wandering_across_pads_is_NOT_a_pet(self):
        """Device-observed 2026-08-27: a single press whose cross-talk ordering
        happened to double back fired PET off nothing but noise (trace
        154411_PET, sessions=1). Pet needs the hand to move BETWEEN contacts."""
        for i, line in enumerate((98, 96, 100)):
            self.hz.touch(line, at_ms=i * 30)
        self.hz.end_session()
        self.assertEqual(self.hz.fired, [])
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["single_click_action"])

    def test_repeated_contact_in_one_place_is_a_double_tap_not_a_pet(self):
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
        # Same pad twice: the case ttp223.py:70-76 documented as a known false
        # positive now resolves to its own gesture.
        self.assertEqual(self.hz.fired, ["mic_toggle_action"])

    def test_a_MULTI_FINGER_double_tap_in_one_place_still_toggles_the_mic(self):
        """Double tap must not need a single fingertip. Three fingers light up
        three pads, but they land TOGETHER — device-measured spread 1-23ms,
        against 53-322ms for a finger that travels. Timing is what separates
        them; pad count cannot."""
        for c in range(2):
            for i, line in enumerate((98, 100, 96)):
                self.hz.touch(line, at_ms=c * 1000 + i * 8)   # ~8ms apart
            self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["mic_toggle_action"])

    def test_a_three_finger_tap_is_one_tap_not_a_swipe(self):
        """Three fingers light every pad at once. Without the timing test that
        looks identical to a hand crossing the surface."""
        for i, line in enumerate((96, 98, 100)):
            self.hz.touch(line, at_ms=i * 8)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["single_click_action"])

    def test_a_contact_that_recorded_no_pads_still_counts(self):
        """Device trace 154507: the second contact's touch edge fell the other
        side of a session boundary, so it recorded only a release and no pads.
        Counting contacts by pad-set would drop it to a single tap; the session
        count is what the gate reads."""
        self.hz.touch(96)
        self.hz.end_session()
        self.hz.end_session()          # a contact that recorded nothing
        self.hz.decide()
        self.assertEqual(self.hz.fired, ["mic_toggle_action"])

    def test_contacts_in_different_places_are_a_pet_not_a_double_tap(self):
        """The other side of the same rule — sharing no pad means the hand
        moved, so multi-pad contacts must not all collapse into double tap."""
        for pads in ((96,), (100,)):
            for line in pads:
                self.hz.touch(line)
            self.hz.end_session()
        self.assertIn("head_pat_action", self.hz.fired)
        self.assertNotIn("mic_toggle_action", self.hz.fired)

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
        self.assertEqual([l for l, _ in self.hz.h._contact], [96, 98, 100])

    def test_release_edges_never_enter_the_sequence(self):
        self.hz.touch(96)
        self.hz.release(96)
        self.hz.touch(98)
        self.assertEqual([l for l, _ in self.hz.h._contact], [96, 98])

    def test_contacts_are_cleared_once_a_gesture_resolves(self):
        """Stale contacts would leak the previous gesture into the next one
        and misclassify it."""
        for i, line in enumerate((96, 98, 100)):
            self.hz.touch(line, at_ms=i * 100)
        self.hz.end_session()
        self.hz.decide()
        self.assertEqual(self.hz.h._contacts, [])
        self.assertEqual(self.hz.h._contact, [])


class TestAxis(_Base):
    swipe = True

    def test_absent_axis_falls_back_to_line_order(self):
        """`axis` is unmeasured until Phase 2.2. A reversal is a reversal
        whichever way the pads are numbered; only direction would be wrong, and
        the driver deliberately does not use direction."""
        self.assertIsNone(self.hz.h._axis)
        for i, line in enumerate((96, 98, 100)):
            self.hz.touch(line, at_ms=i * 100)
        is_swipe, _, _, _ = self.hz.h._classify()
        self.assertTrue(is_swipe)

    def test_a_measured_axis_reorders_the_traversal_test(self):
        """With a real axis where line order != spatial order, the same edge
        sequence reads as a turn rather than a straight pass."""
        hz = _Harness(self.mod, axis=[96, 100, 98])
        for i, line in enumerate((96, 98, 100)):
            hz.touch(line, at_ms=i * 100)
        is_swipe, _, _, _ = hz.h._classify()
        self.assertFalse(is_swipe)


if __name__ == "__main__":
    unittest.main()
