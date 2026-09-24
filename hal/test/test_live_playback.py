"""Hardware AEC playback DSP tests independent of audio hardware."""

import importlib
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from hal.drivers.voice._internal import live_playback as playback


class LivePlaybackTest(unittest.TestCase):
    def setUp(self):
        importlib.reload(playback)
        self.addCleanup(importlib.reload, playback)
        self.enabled = patch.object(playback, 'ENABLED', True)
        self.enabled.start()
        self.addCleanup(self.enabled.stop)
        self.clock = patch.object(playback.time, 'monotonic', return_value=10.0).start()
        self.addCleanup(patch.stopall)

    def test_live_path_uses_existing_software_aec_switch(self):
        from hal.drivers.voice._internal import config
        for live in (False, True):
            for software_aec in (False, True):
                with patch.object(config, 'LIVE_MODE', live), \
                     patch.object(config, 'AEC_ENABLED', software_aec):
                    self.assertEqual(playback._enabled(), live and not software_aec)

    def test_warmup_counts_written_frames_at_each_output_rate(self):
        playback.playback(np.ones(16000, dtype=np.float32), 16000)
        self.assertEqual(playback.played_seconds(), 0)
        playback.record_written(16000, 16000)
        playback.record_written(88200, 44100)
        self.assertEqual(playback.played_seconds(), 3)
        playback.ENABLED = False
        playback.record_written(44100, 44100)
        self.assertEqual(playback.played_seconds(), 3)

    def test_disabled_preserves_object_identity(self):
        playback.ENABLED = False
        for chunk in (b'\0\0', np.ones(40, dtype=np.float32)):
            self.assertIs(playback.playback(chunk, 1000), chunk)
        playback.duck(True)
        self.assertFalse(playback.snapshot()['duck'])

    def test_int16_meter_does_not_overflow_or_measure_ducked_output(self):
        pcm = np.full(40, -32768, dtype=np.int16)
        playback.duck(True)
        out = playback.playback(pcm, 1000)
        self.assertEqual(out.dtype, np.int16)
        self.assertEqual(playback.level(), 1.0)
        self.assertEqual(out[-1], -3932)

    def test_bytes_and_array_pcm_match(self):
        pcm = np.array([-32768, 32767, 0, 16384], dtype=np.int16)
        result = playback.playback(pcm.tobytes(), 1000)
        self.assertIsInstance(result, bytes)
        np.testing.assert_array_equal(np.frombuffer(result, np.int16), pcm)

    def test_float_stereo_retains_layout_and_channels_share_gain(self):
        pcm = np.tile(np.array([0.5, -0.5], dtype=np.float32), (40, 1))
        playback.duck(True)
        result = playback.playback(pcm, 1000)
        self.assertEqual(result.shape, pcm.shape)
        self.assertEqual(result.dtype, pcm.dtype)
        np.testing.assert_array_equal(result[:, 0], -result[:, 1])
        self.assertAlmostEqual(playback.level(), 0.5)
        self.assertAlmostEqual(result[-1, 0], 0.06)

    def test_up_ramp_continues_across_40ms_chunks(self):
        pcm = np.ones(40, dtype=np.float32)
        playback.duck(True)
        down = playback.playback(pcm, 1000)
        self.assertAlmostEqual(down[14], 0.12)
        playback.duck(False)
        first = playback.playback(pcm, 1000)
        self.assertAlmostEqual(first[-1], 0.56)
        second = playback.playback(pcm, 1000)
        self.assertAlmostEqual(second[0] - first[-1], 0.011, places=6)
        self.assertAlmostEqual(second[-1], 1.0)

    def test_down_ramp_continues_across_short_chunks(self):
        playback.duck(True)
        first = playback.playback(np.ones(5, dtype=np.float32), 1000)
        second = playback.playback(np.ones(10, dtype=np.float32), 1000)
        self.assertAlmostEqual(first[-1], 1 - 0.88 / 3, places=6)
        self.assertAlmostEqual(second[0] - first[-1], -0.88 / 15, places=6)
        self.assertAlmostEqual(second[-1], 0.12)

    def test_meter_and_duck_expire_without_capture_refresh(self):
        playback.duck(True)
        playback.playback(np.ones(40, dtype=np.float32), 1000)
        self.clock.return_value = 10.251
        self.assertEqual(playback.level(), 0.0)
        self.assertTrue(playback.snapshot()['duck'])
        self.assertEqual(playback.snapshot()['last_write_age_ms'], 251.0)
        self.clock.return_value = 10.501
        self.assertFalse(playback.snapshot()['duck'])
        out = playback.playback(np.ones(80, dtype=np.float32), 1000)
        self.assertAlmostEqual(out[-1], 1.0)

    def test_speaker_and_aec_receive_identical_ducked_chunk(self):
        from hal.drivers.voice.tts import service
        sink = Mock()
        sink.write.return_value = False
        owner = SimpleNamespace(
            _stream_rate=1000, _audio_written_fired=True,
            _stop_event=threading.Event(), _note_audio_written=Mock(),
            _sd=SimpleNamespace(PortAudioError=RuntimeError),
        )
        stream = service._WatchedStream(sink, owner)
        playback.duck(True)
        with patch.object(service.aec, 'reference_write') as reference:
            stream.write(np.ones(80, dtype=np.float32))
        self.assertEqual(sink.write.call_count, 2)
        for write, ref in zip(sink.write.call_args_list, reference.call_args_list):
            self.assertIs(write.args[0], ref.args[0])
            self.assertAlmostEqual(write.args[0][-1], 0.12)

    def test_softvol_scales_meter_but_never_audio(self):
        pcm = np.ones(40, dtype=np.float32)
        playback.set_mixer_volume(35, -26)
        out = playback.playback(pcm, 1000)
        np.testing.assert_array_equal(out, pcm)
        self.assertAlmostEqual(playback.level(), 10 ** (-26 / 20))
        self.assertAlmostEqual(playback.snapshot()['mixer_gain'], 0.05011872)
        playback.set_mixer_volume(100)
        self.assertEqual(playback.level(), 1.0)

    def test_amixer_readback_updates_only_speaker(self):
        output = 'Front Left: Playback 35 [35%] [-26.00dB]'
        self.assertFalse(playback.observe_mixer('Mic', 'Capture 35 [35%] [-26.00dB]'))
        self.assertEqual(playback.snapshot()['mixer_gain'], 1.0)
        self.assertTrue(playback.observe_mixer('Speaker,0', output))
        self.assertAlmostEqual(playback.snapshot()['mixer_gain'], 10 ** (-26 / 20))
        playback.set_mixer_volume('invalid')
        playback.set_mixer_volume(101)
        self.assertAlmostEqual(playback.snapshot()['mixer_gain'], 10 ** (-26 / 20))

    def test_startup_reads_actual_mixer_when_saved_state_missing(self):
        output = SimpleNamespace(stdout='Playback 35 [35%] [-26.00dB]')
        with patch.object(playback.subprocess, 'run', return_value=output) as run:
            playback.initialize_mixer()
        self.assertEqual(run.call_args.kwargs['timeout'], 1.0)
        self.assertAlmostEqual(playback.snapshot()['mixer_gain'], 10 ** (-26 / 20))
        self.assertEqual(playback.snapshot()['mixer_source'], 'mixer')

    def test_startup_falls_back_to_unity_without_device_db_mapping(self):
        with patch.object(playback.subprocess, 'run', side_effect=FileNotFoundError):
            playback.initialize_mixer()
        self.assertEqual(playback.snapshot()['mixer_gain'], 1.0)
        self.assertEqual(playback.snapshot()['mixer_source'], 'unknown')

    def test_unknown_percentage_does_not_assume_lite_softvol_curve(self):
        playback.set_mixer_volume(35)
        self.assertEqual(playback.snapshot()['mixer_gain'], 1.0)
        self.assertTrue(playback.observe_mixer('PCM', 'Playback 77 [77%] [-8.00dB]'))
        self.assertAlmostEqual(playback.snapshot()['mixer_gain'], 10 ** (-8 / 20))


class LiveRealtimeStopTest(unittest.TestCase):
    def make_service(self, speaking, realtime):
        from hal.drivers.voice.tts.service import TTSService, _PendingSpeech
        tts = object.__new__(TTSService)
        tts._pending_queue_lock = threading.Lock()
        tts._stop_event = threading.Event()
        tts._wake_drain_queues = Mock()
        tts._speaking = speaking
        tts._native_mode = False
        tts._realtime_reply = realtime
        live = _PendingSpeech('live', False, realtime_reply=True)
        main = _PendingSpeech('main', False, realtime_feedback=True)
        other = _PendingSpeech('notice', False)
        tts._pending_queue = [live, main, other]
        return tts, live, main, other

    def test_idle_gap_cancels_queued_live_without_discarding_main(self):
        tts, live, main, other = self.make_service(False, True)
        tts.stop_realtime_reply()
        self.assertTrue(live.cancelled.is_set())
        self.assertEqual(tts._pending_queue, [main, other])
        self.assertFalse(tts._stop_event.is_set())
        tts._wake_drain_queues.assert_not_called()

    def test_active_main_and_pending_main_are_preserved(self):
        tts, live, main, other = self.make_service(True, False)
        tts._active_pending_speech = main
        tts.stop_realtime_reply()
        self.assertTrue(live.cancelled.is_set())
        self.assertFalse(main.cancelled.is_set())
        self.assertEqual(tts._pending_queue, [main, other])
        self.assertFalse(tts._stop_event.is_set())
        tts._wake_drain_queues.assert_not_called()

    def test_active_live_is_cancelled_and_worker_resumes_preserved_queue(self):
        tts, live, main, other = self.make_service(True, True)
        tts._active_pending_speech = live
        tts.stop_realtime_reply()
        self.assertTrue(live.cancelled.is_set())
        self.assertTrue(tts._stop_event.is_set())
        self.assertTrue(tts._resume_pending_after_stop)
        self.assertEqual(tts._pending_queue, [main, other])
        tts._wake_drain_queues.assert_called_once()


if __name__ == '__main__':
    unittest.main()
