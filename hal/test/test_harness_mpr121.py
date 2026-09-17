"""Harness touch policy never invokes normal-mode destructive actions."""
import unittest
from unittest.mock import Mock, patch

from hal.drivers.harness_mpr121 import HarnessGestures, harness_button_recognizer
from hal.drivers.mpr121 import _GestureEvent, _SpatialGestureRecognizer, MPR121Handler
from hal.board.mpr121 import MPR121Config


class HarnessGestureTests(unittest.TestCase):
    def test_each_tap_is_immediate_without_triple_or_cue(self):
        detector = harness_button_recognizer(0)
        detector.update(False, 0)
        events = []
        for index in range(3):
            events += detector.update(True, 1 + index * .2)
            events += detector.update(False, 1.1 + index * .2)
        events += detector.update(False, 3)
        self.assertEqual([e.kind for e in events if e.kind in ('single', 'cue', 'triple', 'hold')], ['single'] * 3)

    def test_hold_has_only_one_tier_and_exits_on_release(self):
        detector = harness_button_recognizer(0)
        detector.update(False, 0)
        events = detector.update(True, 1)
        for now in (3.9, 4, 6, 11, 20):
            events += detector.update(True, now)
        self.assertEqual([e.count for e in events if e.kind == 'hold_tier'], [1])
        self.assertFalse(any(e.kind == 'hold' for e in events))
        self.assertEqual([e.kind for e in detector.update(False, 21)], ['release', 'invalidate', 'hold'])

    def test_spatial_detector_preserves_manual_taps(self):
        detector = _SpatialGestureRecognizer(MPR121Config(bus=0, swipe_axis=(0, 1, 2, 3)), harness_button_recognizer)
        detector.update(0, 0)
        events = []
        for now, mask in [(1, 1), (1.04, 1), (1.1, 0), (1.24, 0), (1.3, 1), (1.34, 1), (1.4, 0), (1.54, 0), (2, 0)]:
            events += detector.update(mask, now)
        self.assertEqual([e.kind for e in events if e.kind in ('single', 'cue', 'triple')], ['single', 'single'])

    def test_tts_tap_only_interrupts_then_taps_open_and_finish_capture(self):
        gestures = HarnessGestures()
        gestures.snapshot = {'enabled': True, 'generation': 1, 'focusAvailable': True}
        voice, tts = Mock(), Mock()
        with patch('hal.drivers.harness_mpr121.state.voice_service', voice), \
                patch('hal.drivers.harness_mpr121.state.tts_service', tts), \
                patch('hal.drivers.harness_mpr121.state._hw_mic_switch_muted', False), \
                patch('hal.drivers.button_actions._cancel_agent_speech') as cancel, \
                patch('hal.routes.voice.stop_tts') as stop:
            tts.speaking = True
            gestures.execute(_GestureEvent('single', 1))
            stop.assert_called_once()
            cancel.assert_called_once()
            voice.start_harness_capture.assert_not_called()
            tts.speaking = False
            voice.harness_capture_active = False
            gestures.execute(_GestureEvent('single', 2))
            voice.start_harness_capture.assert_called_once_with(gestures.snapshot)
            voice.harness_capture_active = True
            gestures.execute(_GestureEvent('single', 3))
            voice.finish_harness_capture.assert_called_once()

    def test_harness_ignores_legacy_actions_and_long_holds_only_disable(self):
        gestures = HarnessGestures()
        with patch.object(gestures, '_disable') as disable, patch.object(gestures, '_tap') as tap:
            for kind in ('cue', 'triple', 'hold_tier'):
                gestures.execute(_GestureEvent(kind, 1, count=3, held_s=15))
            gestures.execute(_GestureEvent('hold', 2, held_s=2.99))
            disable.assert_not_called()
            tap.assert_not_called()
            gestures.execute(_GestureEvent('hold', 3, held_s=12))
            disable.assert_called_once()

    def test_mode_change_discards_contact_and_queued_legacy_actions(self):
        handler = MPR121Handler(MPR121Config(bus=0))
        gestures = HarnessGestures()
        gestures.snapshot = {'enabled': True, 'generation': 2}
        handler._harness_gestures = gestures
        handler._pending.put((0, _GestureEvent('triple', 1), 0))
        handler._process_touch(True, 1)
        self.assertTrue(handler._pending.empty())
        handler._process_touch(False, 12)
        handler._process_touch(False, 12.04)
        self.assertTrue(handler._pending.empty())
        with patch('hal.drivers.harness_mpr121.read_mode', return_value={'enabled': False, 'generation': 3}), \
                patch.object(gestures, 'cancel_capture'), patch('hal.drivers.mpr121.triple_click_action') as reboot:
            handler._execute(_GestureEvent('triple', 1))
            reboot.assert_not_called()

    def test_unavailable_status_blocks_legacy_actions(self):
        handler = MPR121Handler(MPR121Config(bus=0))
        gestures = HarnessGestures()
        handler._harness_gestures = gestures
        with patch('hal.drivers.harness_mpr121.read_mode', return_value={'enabled': False, 'unavailable': True}), \
                patch('hal.drivers.mpr121.hold_release_action') as hold:
            handler._execute(_GestureEvent('hold', 1, held_s=15))
            hold.assert_not_called()

    def test_swipes_step_both_directions_and_cancel_recording(self):
        gestures = HarnessGestures()
        gestures.snapshot = {'enabled': True, 'generation': 10, 'agentId': 'a'}
        result = {'enabled': True, 'generation': 11, 'agentId': 'b', 'agentName': 'Agent B'}
        with patch.object(gestures, 'cancel_capture') as cancel, \
                patch('hal.drivers.harness_mpr121.request_focus_step', return_value=result) as step, \
                patch('hal.drivers.button_actions._speak_gesture_ack'):
            gestures.execute(_GestureEvent('swipe', 1, direction=-1))
            self.assertEqual(step.call_args.args[1:], ('next', 10))
            gestures.execute(_GestureEvent('swipe', 2, direction=1))
            self.assertEqual(step.call_args.args[1:], ('previous', 11))
            self.assertGreaterEqual(cancel.call_count, 2)

    def test_exit_uses_explicit_disable_even_if_mode_changed(self):
        gestures = HarnessGestures()
        with patch.object(gestures, 'cancel_capture'), \
                patch('hal.drivers.harness_mpr121.request_voice_disable', return_value={'enabled': False}) as disable, \
                patch('hal.drivers.harness_mpr121._show_feedback'), \
                patch('hal.drivers.button_actions._speak_gesture_ack'):
            gestures.execute(_GestureEvent('hold', 1, held_s=3))
            disable.assert_called_once()
            self.assertFalse(gestures.snapshot['enabled'])

    def test_focus_phrases_support_all_languages(self):
        from hal.i18n import PHRASES_BY_LANG, PHRASE_HARNESS_FOCUS, PHRASE_HARNESS_FOCUS_FAILED
        for phrase in (PHRASE_HARNESS_FOCUS, PHRASE_HARNESS_FOCUS_FAILED):
            self.assertEqual(len(PHRASES_BY_LANG[phrase]), 4)
            for text in PHRASES_BY_LANG[phrase].values():
                self.assertTrue(text.format(agent='Test Agent'))
