"""MPR121 register setup, touch grouping, and worker lifecycle without hardware."""

import threading
import time
import unittest
from unittest import mock

from hal.board.mpr121 import MPR121Config
from hal.drivers.mpr121 import I2CBus, MPR121Handler, _GestureRecognizer, _GestureEvent


class TestGestures(unittest.TestCase):
    def events(self, samples, debounce_ms=0):
        detector = _GestureRecognizer(debounce_ms)
        return [event for sample in samples for event in detector.update(*sample)]

    def actions(self, events):
        return [event for event in events if event.kind in ('single', 'cue', 'triple', 'hold')]

    def test_one_two_three_four_taps(self):
        for count in range(1, 5):
            with self.subTest(count=count):
                samples = [(False, 0)]
                for index in range(count):
                    samples += [(True, 1 + index * .2), (False, 1.1 + index * .2)]
                samples += [(False, samples[-1][1] + .41)]
                actions = self.actions(self.events(samples))
                self.assertEqual([event.kind for event in actions], ['single', 'triple' if count == 3 else 'cue'])
                self.assertEqual(actions[-1].count, count)

    def test_exact_click_window_boundary(self):
        detector = _GestureRecognizer(0)
        for sample in [(False, 0), (True, .5), (False, 1)]:
            detector.update(*sample)
        self.assertEqual(detector.update(False, 1.399), [])
        self.assertEqual([event.kind for event in detector.update(False, 1.4)], ['cue'])
        events = detector.update(True, 1.5) + detector.update(False, 1.6) + detector.update(False, 2)
        self.assertEqual([event.kind for event in self.actions(events)], ['single', 'cue'])

    def test_hold_boundaries_and_no_action_while_held(self):
        for duration in (1.999, 2, 4.999, 5, 9.999, 10, 15):
            with self.subTest(duration=duration):
                detector = _GestureRecognizer(0)
                detector.update(False, 0)
                detector.update(True, 1)
                self.assertEqual(self.actions(detector.update(True, 1 + duration)), [])
                actions = self.actions(detector.update(False, 1 + duration))
                self.assertEqual([event.kind for event in actions], ['single' if duration < 2 else 'hold'])
                self.assertAlmostEqual(actions[0].held_s, duration)

    def test_hold_clears_pending_triple_and_cue(self):
        samples = [(False, 0)]
        for index in range(3):
            samples += [(True, 1 + index * .2), (False, 1.1 + index * .2)]
        samples += [(True, 1.7), (True, 4), (False, 4.7), (False, 6)]
        self.assertEqual([event.kind for event in self.actions(self.events(samples))], ['single', 'hold'])

    def test_chatter_does_not_count_or_change_duration(self):
        samples = [(False, 0), (True, 1), (False, 1.01), (False, 1.1),
                   (True, 2), (True, 2.04), (False, 3), (True, 3.01),
                   (True, 3.1), (False, 4), (False, 4.04), (False, 5)]
        actions = self.actions(self.events(samples, 30))
        self.assertEqual([event.kind for event in actions], ['hold'])
        self.assertEqual(actions[0].held_s, 2)

    def test_debounce_does_not_promote_short_hold(self):
        samples = [(False, 0), (True, 1), (True, 1.04), (False, 2.999), (False, 3.04)]
        actions = self.actions(self.events(samples, 30))
        self.assertEqual([event.kind for event in actions], ['single'])
        self.assertAlmostEqual(actions[0].held_s, 1.999)

    def test_boot_hold_suppressed_until_stable_release(self):
        samples = [(True, 0), (True, 10), (False, 11), (True, 11.01),
                   (False, 12), (False, 12.04), (True, 13), (True, 13.04),
                   (False, 13.2), (False, 13.24), (False, 14)]
        self.assertEqual([event.kind for event in self.actions(self.events(samples, 30))], ['single', 'cue'])

    def test_cancel_clears_pending_burst_and_hold(self):
        detector = _GestureRecognizer(0)
        for sample in [(False, 0), (True, 1), (False, 1.1)]:
            detector.update(*sample)
        detector.cancel()
        self.assertEqual(self.actions(detector.update(False, 10)), [])
        detector = _GestureRecognizer(0)
        detector.update(False, 0)
        detector.update(True, 1)
        detector.cancel()
        self.assertEqual(self.actions(detector.update(False, 20)), [])

    def test_hold_tiers_are_logged_once_per_threshold(self):
        events = self.events([(False, 0), (True, 1), (True, 3), (True, 4), (True, 6), (True, 11)])
        self.assertEqual([event.count for event in events if event.kind == 'hold_tier'], [1, 2, 3])


class TestMPR121(unittest.TestCase):
    def make_handler(self, **kwargs):
        return MPR121Handler(MPR121Config(bus=5, **kwargs))

    def test_register_initialization(self):
        handler = self.make_handler()
        handler._bus = bus = mock.Mock()
        bus.read_regs.return_value = b'\x24'
        handler._initialize()
        calls = bus.write_reg.call_args_list
        self.assertEqual(calls[:2], [mock.call(0x5A, 0x80, 0x63), mock.call(0x5A, 0x5E, 0)])
        for electrode in range(12):
            self.assertIn(mock.call(0x5A, 0x41 + electrode * 2, 2), calls)
            self.assertIn(mock.call(0x5A, 0x42 + electrode * 2, 1), calls)
        for register, value in ((0x5B, 0), (0x5C, 0x10), (0x5D, 0x20),
                                (0x7D, 200), (0x7F, 180), (0x7E, 130), (0x7B, 0x0B)):
            self.assertIn(mock.call(0x5A, register, value), calls)
        self.assertEqual(calls[-1], mock.call(0x5A, 0x5E, 0x8F))

    def test_autoconfig_disabled(self):
        handler = self.make_handler(autoconfig=False)
        handler._bus = mock.Mock()
        handler._bus.read_regs.return_value = b'\x24'
        handler._initialize()
        self.assertFalse(any(call.args[1] == 0x7B for call in handler._bus.write_reg.call_args_list))

    def test_selected_electrodes_overlap_form_one_touch(self):
        handler = self.make_handler(electrodes=(0, 1))
        handler._bus = mock.Mock()
        # Electrode 2 is excluded. Overlapping 0 and 1 must not split the tap.
        masks = [4, 1, 1, 3, 2, 2, 4, 4]
        handler._bus.read_regs.side_effect = [mask.to_bytes(2, 'little') for mask in masks]
        times = [0, 1, 1.04, 2, 3, 3.04, 4, 4.04]
        events = [event for now in times for event in handler._detector.update(handler._read_touched(), now)]
        holds = [event for event in events if event.kind == 'hold']
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0].held_s, 3)

    def test_electrode_logs_include_changes_without_repeating_samples(self):
        handler = self.make_handler(electrodes=(0,))
        handler._bus = mock.Mock()
        handler._bus.read_regs.side_effect = [mask.to_bytes(2, 'little') for mask in (0, 1, 1, 3, 2)]
        with self.assertLogs('hal.drivers.mpr121', level='INFO') as logs:
            results = [handler._read_touched() for _ in range(5)]
        self.assertEqual(results, [False, True, True, True, False])
        self.assertEqual(len(logs.output), 4)
        self.assertIn('raw_mask=0x003 selected_mask=0x001 selected_active=0x001 touched=[1] released=[]', logs.output[2])
        self.assertIn('raw_mask=0x002 selected_mask=0x001 selected_active=0x000 touched=[] released=[0]', logs.output[3])

    def test_overcurrent_is_fault_not_release(self):
        handler = self.make_handler()
        handler._bus = mock.Mock()
        handler._bus.read_regs.return_value = b"\x00\x80"
        with self.assertRaisesRegex(OSError, "over-current"):
            handler._read_touched()

    def test_init_failure_closes_bus(self):
        handler = self.make_handler()
        bus = mock.Mock()
        bus.read_regs.return_value = b'\x00'
        with mock.patch('hal.drivers.mpr121.I2CBus', return_value=bus) as factory:
            with self.assertRaises(RuntimeError):
                handler.start()
        factory.assert_called_once_with(5)
        bus.close.assert_called_once()
        handler.stop()
        bus.close.assert_called_once()

    def test_initial_touch_read_failure_closes_bus(self):
        handler = self.make_handler()
        bus = mock.Mock()
        bus.read_regs.side_effect = [b'\x24', OSError('touch read failed')]
        with mock.patch('hal.drivers.mpr121.I2CBus', return_value=bus), mock.patch('hal.drivers.mpr121.time.sleep'):
            with self.assertRaises(OSError):
                handler.start()
        bus.close.assert_called_once()

    def test_semantic_actions_call_shared_functions(self):
        handler = self.make_handler()
        with mock.patch('hal.drivers.mpr121.single_click_action') as single, \
                mock.patch('hal.drivers.mpr121.announce_listening_cue') as cue, \
                mock.patch('hal.drivers.mpr121.triple_click_action') as triple, \
                mock.patch('hal.drivers.mpr121.hold_release_action') as hold:
            for event in [_GestureEvent('single', 1), _GestureEvent('cue', 1),
                          _GestureEvent('triple', 2, count=3), _GestureEvent('hold', 3, held_s=10)]:
                handler._execute(event)
        single.assert_called_once_with(source='MPR121', announce=False)
        cue.assert_called_once_with(source='MPR121')
        triple.assert_called_once_with(source='MPR121')
        hold.assert_called_once_with(10, source='MPR121')

    def test_worker_delivers_action_with_correlation_logs(self):
        handler = self.make_handler()
        called = threading.Event()
        with mock.patch('hal.drivers.mpr121.single_click_action', side_effect=lambda **kwargs: called.set()) as action, \
                self.assertLogs('hal.drivers.mpr121', level='INFO') as logs:
            handler._action_thread = threading.Thread(target=handler._dispatch)
            handler._action_thread.start()
            try:
                handler._process_touch(False, 0)
                handler._process_touch(True, 1)
                handler._process_touch(True, 1.04)
                handler._process_touch(False, 1.2)
                handler._process_touch(False, 1.24)
                self.assertTrue(called.wait(1))
            finally:
                handler.stop()
            action.assert_called_once_with(source='MPR121', announce=False)
        output = '\n'.join(logs.output)
        for event in ('action_queued', 'action_begin', 'action_complete'):
            self.assertIn(f'event={event} gesture_id=1 action=single', output)

    def test_poll_failure_closes_bus_and_stops_dispatch(self):
        handler = self.make_handler(poll_ms=1)
        bus = mock.Mock()
        bus.read_regs.side_effect = [b'\x24', b'\x00\x00', OSError('disconnected')]
        with mock.patch('hal.drivers.mpr121.I2CBus', return_value=bus):
            handler.start()
            self.assertTrue(handler._stop.wait(1))
            handler.stop()
        bus.close.assert_called_once()
        self.assertFalse(handler._poll_thread.is_alive())
        self.assertFalse(handler._action_thread.is_alive())

    def test_poll_fault_cancels_pending_gesture_and_actions(self):
        handler = self.make_handler(poll_ms=1, debounce_ms=0)
        handler._bus = bus = mock.Mock()
        handler._process_touch(False, 0)
        handler._process_touch(True, 1)
        handler._process_touch(False, 1.1)
        self.assertFalse(handler._pending.empty())
        with mock.patch.object(handler, '_read_touched', side_effect=OSError('disconnected')), \
                self.assertLogs('hal.drivers.mpr121', level='ERROR'):
            handler._poll()
        self.assertTrue(handler._stop.is_set())
        self.assertTrue(handler._pending.empty())
        self.assertEqual(handler._detector.update(False, 10), [])
        bus.close.assert_called_once()

    def test_stop_closes_bus_once(self):
        handler = self.make_handler()
        bus = mock.Mock()
        bus.read_regs.side_effect = lambda address, register, length: b'\x24' if register == 0x5D else b'\x00\x00'
        with mock.patch('hal.drivers.mpr121.I2CBus', return_value=bus):
            handler.start()
            handler.stop()
            handler.stop()
        bus.close.assert_called_once()

    def test_busy_worker_rejects_destructive_outcomes(self):
        for kind in ('hold', 'triple'):
            with self.subTest(kind=kind):
                handler = self.make_handler()
                handler._action_busy = True
                handler._detector = mock.Mock()
                handler._detector.update.return_value = [_GestureEvent(kind, 1, held_s=10)]
                with self.assertLogs('hal.drivers.mpr121', level='WARNING') as logs:
                    handler._process_touch(False, 10)
                self.assertTrue(handler._pending.empty())
                self.assertIn('reason=action_worker_busy', logs.output[0])

    def test_new_touch_invalidates_pending_destructive_outcome(self):
        handler = self.make_handler()
        handler._process_touch(False, 0)
        handler._pending.put_nowait((handler._generation, _GestureEvent('hold', 1, held_s=10), time.monotonic()))
        handler._process_touch(True, 1)
        self.assertTrue(handler._pending.empty())

    def test_expired_destructive_outcome_never_executes(self):
        handler = self.make_handler()
        handler._pending.put_nowait((handler._generation, _GestureEvent('triple', 1, count=3), time.monotonic() - 1))
        with mock.patch.object(handler, '_execute') as execute, \
                self.assertLogs('hal.drivers.mpr121', level='INFO') as logs:
            handler._action_thread = threading.Thread(target=handler._dispatch)
            handler._action_thread.start()
            # Observing an empty queue means the worker has consumed the request;
            # stop then joins it before assertions about execution/logging.
            deadline = time.monotonic() + 1
            while not handler._pending.empty() and time.monotonic() < deadline:
                threading.Event().wait(.001)
            handler.stop()
        execute.assert_not_called()
        self.assertTrue(any('reason=stale_expired_or_stopping' in item for item in logs.output))

    def test_stop_discards_pending_actions(self):
        handler = self.make_handler()
        handler._pending.put_nowait((handler._generation, _GestureEvent('triple', 1, count=3), time.monotonic()))
        with mock.patch.object(handler, '_execute') as execute:
            handler.stop()
            handler._dispatch()
        self.assertTrue(handler._pending.empty())
        execute.assert_not_called()

    def test_queue_capacity_is_bounded_without_affecting_gesture_count(self):
        handler = self.make_handler()
        handler._detector = mock.Mock()
        handler._detector.update.return_value = [_GestureEvent('cue', index) for index in range(10)]
        with self.assertLogs('hal.drivers.mpr121', level='WARNING'):
            handler._process_touch(False, 1)
        self.assertEqual(handler._pending.qsize(), 2)

    def test_transport_close_is_idempotent(self):
        bus = I2CBus.__new__(I2CBus)
        bus.fd = 42
        with mock.patch('hal.drivers.mpr121.os.close') as close:
            bus.close()
            bus.close()
        close.assert_called_once_with(42)
