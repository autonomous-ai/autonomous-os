"""A claimed voice chord must never become a destructive button gesture."""

import unittest
from hal.board.mpr121 import MPR121Config
from hal.drivers.mpr121 import MPR121Handler

PAIR = (1 << 0) | (1 << 11)
ACTIONS = {"single", "cue", "triple", "hold", "hold_tier", "swipe", "harness_voice"}


def replay(samples, startup=0, spatial=True):
    config = MPR121Config(bus=0, swipe_axis=tuple(range(12)) if spatial else None, harness_voice_chord=(0, 11))
    detector = MPR121Handler(config)._detector
    detector.update(startup, -1)
    events, index, mask = [], 0, startup
    for tick in range(int((samples[-1][0] + .7) * 1000) + 1):
        now = tick / 1000
        while index < len(samples) and samples[index][0] <= now:
            mask = samples[index][1]
            index += 1
        events.extend(detector.update(mask, now))
    return [e.kind for e in events if e.kind in ACTIONS]


class HarnessChordTests(unittest.TestCase):
    def test_pair_fires_while_held_once_and_requires_full_release(self):
        self.assertEqual(replay([(1, PAIR), (15, 1), (20, PAIR), (25, 0),
                                 (26, PAIR), (28, 0)]), ["harness_voice"] * 2)

    def test_threshold_and_debounce_do_not_promote_short_hold(self):
        for duration in (0.001, 1.49, 1.529):
            self.assertEqual(replay([(1, PAIR), (1 + duration, 0)]), [])
        self.assertEqual(replay([(1, PAIR), (2.54, 0)]), ["harness_voice"])

    def test_third_pad_partial_release_and_retouch_cancel_without_fallback(self):
        for middle in (PAIR | 2, 1, 0):
            self.assertEqual(replay([(1, PAIR), (1.5, middle), (1.51, PAIR), (15, 0)]), [])
        self.assertEqual(replay([(1, PAIR | 2), (2, PAIR), (15, 0)]), [])

    def test_startup_held_suppressed_until_full_release(self):
        self.assertEqual(replay([(2, PAIR), (15, 0), (16, PAIR), (18, 0)], startup=PAIR),
                         ["harness_voice"])

    def test_sequential_fingers_claim_existing_contact(self):
        self.assertEqual(replay([(1, 1), (1.15, PAIR), (3, 0)]), ["harness_voice"])

    def test_normal_tap_after_chord_release_works(self):
        for spatial in (True, False):
            self.assertEqual(replay([(1, PAIR), (3, 0), (4, 4), (4.1, 0)], spatial=spatial),
                             ["harness_voice", "single", "cue"])

    def test_swipe_after_chord_release_works(self):
        samples = [(1, PAIR), (3, 0)]
        samples += [(4 + pad * .04, (1 << pad) | (1 << (pad + 1))) for pad in range(8)]
        samples += [(4.4, 0)]
        self.assertEqual(replay(samples), ["harness_voice", "swipe"])

    def test_cancel_on_hardware_error_blocks_held_chord(self):
        config = MPR121Config(bus=0, harness_voice_chord=(0, 11))
        detector = MPR121Handler(config)._detector
        detector.update(0, 0)
        detector.update(PAIR, 1)
        detector.cancel()
        self.assertEqual(detector.update(PAIR, 20), [])

    def test_pair_validation(self):
        for pair in ([0], [0, 0], [0, 12], [True, 0], "01"):
            with self.assertRaises(ValueError):
                MPR121Config(bus=0, harness_voice_chord=pair)
        self.assertEqual(MPR121Config(bus=0, harness_voice_chord=[0, 11]).harness_voice_chord, (0, 11))
