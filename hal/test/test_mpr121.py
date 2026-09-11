"""MPR121 register setup, touch grouping, and worker lifecycle without hardware."""

import threading
import unittest
from unittest import mock

from hal.board.mpr121 import MPR121Config
from hal.drivers.mpr121 import I2CBus, MPR121Handler, _TapDetector


class TestTapDetector(unittest.TestCase):
    def test_one_release_one_click_even_after_long_hold(self):
        detector = _TapDetector(30)
        samples = [(False, 0), (True, 1), (True, 1.04), (True, 20),
                   (False, 21), (False, 21.04), (False, 22)]
        self.assertEqual([detector.update(*sample) for sample in samples],
                         [False, False, False, False, False, True, False])

    def test_chatter_does_not_click_or_split_touch(self):
        detector = _TapDetector(30)
        samples = [(False, 0), (True, 1), (False, 1.01), (False, 1.1),
                   (True, 2), (True, 2.04), (False, 3), (True, 3.01),
                   (True, 3.1), (False, 4), (False, 4.04)]
        self.assertEqual(sum(detector.update(*sample) for sample in samples), 1)

    def test_boot_hold_is_suppressed_until_stable_release(self):
        detector = _TapDetector(30)
        samples = [(True, 0), (True, 10), (False, 11), (True, 11.01),
                   (False, 12), (False, 12.04), (True, 13), (True, 13.04),
                   (False, 14), (False, 14.04)]
        results = [detector.update(*sample) for sample in samples]
        self.assertEqual(results, [False] * 9 + [True])

    def test_rapid_separate_taps_remain_single_clicks(self):
        detector = _TapDetector(30)
        samples = [(False, 0), (True, .1), (True, .14), (False, .2),
                   (False, .24), (True, .3), (True, .34), (False, .4), (False, .44)]
        self.assertEqual(sum(detector.update(*sample) for sample in samples), 2)


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
        self.assertEqual(sum(handler._detector.update(handler._read_touched(), now) for now in times), 1)

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

    def test_poll_delivers_tap_to_shared_single_click(self):
        handler = self.make_handler(poll_ms=1)
        bus = mock.Mock()
        statuses = iter([0, 1, 1, 0, 0])
        times = iter([0, 1, 1.04, 2, 2.04])
        bus.read_regs.side_effect = lambda address, register, length: (
            b'\x24' if register == 0x5D else next(statuses, 0).to_bytes(2, 'little')
        )
        called = threading.Event()
        with mock.patch('hal.drivers.mpr121.I2CBus', return_value=bus), \
                mock.patch('hal.drivers.mpr121.time.sleep'), \
                mock.patch('hal.drivers.mpr121.time.monotonic', side_effect=lambda: next(times, 3)), \
                mock.patch('hal.drivers.mpr121.single_click_action', side_effect=lambda **kwargs: called.set()) as action, \
                self.assertLogs('hal.drivers.mpr121', level='INFO') as logs:
            handler.start()
            try:
                self.assertTrue(called.wait(1))
            finally:
                handler.stop()
            action.assert_called_once_with(source='MPR121')
        output = '\n'.join(logs.output)
        for event in ('tap_accepted', 'action_queued', 'action_begin', 'action_complete'):
            self.assertIn(f'event={event} tap_id=1', output)
        self.assertIn('event=worker_stopped worker=action', output)

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

    def test_stop_closes_bus_once(self):
        handler = self.make_handler()
        bus = mock.Mock()
        bus.read_regs.side_effect = lambda address, register, length: b'\x24' if register == 0x5D else b'\x00\x00'
        with mock.patch('hal.drivers.mpr121.I2CBus', return_value=bus):
            handler.start()
            handler.stop()
            handler.stop()
        bus.close.assert_called_once()

    def test_action_dispatch_is_bounded_and_discards_pending_on_stop(self):
        handler = self.make_handler()
        entered = threading.Event()
        release = threading.Event()

        def action(**kwargs):
            entered.set()
            release.wait(1)

        with mock.patch('hal.drivers.mpr121.single_click_action', side_effect=action) as callback:
            handler._action_thread = threading.Thread(target=handler._dispatch)
            handler._action_thread.start()
            handler._pending.put_nowait(True)
            self.assertTrue(entered.wait(1))
            handler._pending.put_nowait(True)
            self.assertTrue(handler._pending.full())
            handler._stop.set()
            release.set()
            handler.stop()
        callback.assert_called_once_with(source='MPR121')

    def test_transport_close_is_idempotent(self):
        bus = I2CBus.__new__(I2CBus)
        bus.fd = 42
        with mock.patch('hal.drivers.mpr121.os.close') as close:
            bus.close()
            bus.close()
        close.assert_called_once_with(42)
