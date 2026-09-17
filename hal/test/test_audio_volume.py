"""Regression tests for USB mixers with duplicate playback control names."""
import subprocess
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from hal.models import VolumeRequest
from hal.routes import audio


CONTROLS = """Simple mixer control 'PCM',0
  Capabilities: pvolume pswitch
Simple mixer control 'PCM',1
  Capabilities: pvolume pswitch
Simple mixer control 'Headset',0
  Capabilities: cvolume cswitch
Simple mixer control 'Mic',0
  Capabilities: pvolume cvolume
"""


class AudioVolumeTests(unittest.TestCase):
    def setUp(self):
        for obj, name, value in (
            (audio, "AUDIO_OUTPUT_ALSA", "plug:device_speaker"),
            (audio, "_bt_sink", lambda: None),
            (audio.state, "simulation_audio", False),
            (audio.state, "safety_policy", None),
        ):
            patcher = patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(audio, "_persist_volume")
        self.persist = patcher.start()
        self.addCleanup(patcher.stop)

    def test_both_pcm_indices_written_capture_controls_untouched(self):
        with patch.object(audio.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], 0, CONTROLS, "")) as run:
            self.assertEqual(audio.set_volume(VolumeRequest(volume=77))["volume"], 77)
        writes = [call.args[0] for call in run.call_args_list if "sset" in call.args[0]]
        self.assertEqual(writes, [
            ["amixer", "-D", "device_speaker", "sset", "PCM", "--", "77%"],
            ["amixer", "-D", "device_speaker", "sset", "PCM,1", "--", "77%"],
        ])
        self.assertTrue(all(call.kwargs["check"] for call in run.call_args_list
                            if "sset" in call.args[0]))
        self.persist.assert_called_once_with(77)

    def test_failed_second_write_does_not_report_or_persist_success(self):
        for error in (subprocess.CalledProcessError(1, "amixer"),
                      subprocess.TimeoutExpired("amixer", 5), OSError("unavailable")):
            with self.subTest(error=error):
                with patch.object(audio.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, CONTROLS, ""),
                    subprocess.CompletedProcess([], 0, "", ""), error,
                ]):
                    with self.assertRaises(HTTPException) as raised:
                        audio.set_volume(VolumeRequest(volume=77))
                self.assertEqual(raised.exception.status_code, 503)
                self.persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
