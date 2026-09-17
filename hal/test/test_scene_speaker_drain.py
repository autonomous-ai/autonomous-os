"""A scene's confirmation line must survive the scene muting the speaker.

The /scene marker lands before the reply text; an inline mute swallowed the
line and, chained with sleepy, left sleep without ownership of the mute
(device-observed 2026-09-17, lamp-0c89).
"""
import os, sys, threading, time, unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from hal.test.test_sleepy_speaker_drain import FakeTTS  # noqa: E402


class SceneSpeakerDrainTest(unittest.TestCase):
    def setUp(self):
        from hal import app_state as state, privacy
        from hal.routes import scene
        self.state, self.privacy, self.scene = state, privacy, scene
        self._saved = {k: getattr(state, k) for k in (
            "SLEEPY_SPEAKER_GRACE_S", "SLEEPY_SPEAKER_DRAIN_MAX_S", "_SLEEPY_DRAIN_POLL_S",
            "tts_service", "music_service", "voice_service", "rgb_service",
            "sensing_service", "_stop_current_effect",
            "animation_service", "camera_capture", "_current_emotion", "_sleeping",
            "_speaker_muted", "_mic_muted", "_camera_disabled", "_active_scene",
            "_sleepy_auto_muted_speaker", "_sleepy_auto_muted_mic", "_save_boot_sidecar",
            "_save_user_led_state",
        )}
        self._saved_privacy = {k: getattr(privacy, k) for k in (
            "camera_muted", "speaker_muted", "camera_before", "speaker_before")}
        state.SLEEPY_SPEAKER_GRACE_S = 0.20
        state.SLEEPY_SPEAKER_DRAIN_MAX_S = 1.0
        state._SLEEPY_DRAIN_POLL_S = 0.01
        state.music_service = None
        state.voice_service = None
        state.rgb_service = mock.Mock()  # activate_scene refuses without an LED strip
        state.sensing_service = None
        state._stop_current_effect = mock.Mock()
        state.animation_service = None
        state.camera_capture = None
        state._current_emotion = "idle"
        state._sleeping = False
        state._speaker_muted = False
        state._mic_muted = False
        state._camera_disabled = False
        state._active_scene = None
        state._sleepy_auto_muted_speaker = False
        state._sleepy_auto_muted_mic = False
        state._save_boot_sidecar = mock.Mock()
        state._save_user_led_state = mock.Mock()
        for k in self._saved_privacy:
            setattr(privacy, k, False if k.endswith("muted") else None)
        self._persist = mock.patch.object(scene, "_persist_scene")
        self._persist.start()

    def tearDown(self):
        state = self.state
        state._cancel_scene_speaker_drain()
        state._cancel_sleepy_speaker_drain()
        self._persist.stop()
        for k, v in self._saved.items():
            setattr(state, k, v)
        for k, v in self._saved_privacy.items():
            setattr(self.privacy, k, v)

    def _activate(self, name="night"):
        from hal.models import SceneRequest
        self.scene.activate_scene(SceneRequest(scene=name))

    def _wait_for_mute(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state._speaker_muted:
                return True
            time.sleep(0.01)
        return False

    # --- the bug this exists for -------------------------------------------

    def test_confirmation_line_arriving_after_the_marker_still_plays(self):
        state = self.state
        tts = FakeTTS()
        state.tts_service = tts
        tts.speak_after(delay_s=0.06, duration_s=0.25)

        self._activate("night")

        self.assertFalse(state._speaker_muted, "speaker muted before the line arrived")
        time.sleep(0.15)
        self.assertFalse(state._speaker_muted, "speaker muted mid-line")
        self.assertEqual(tts.stop_calls, 0, "in-flight confirmation was cut off")
        self.assertTrue(self._wait_for_mute(), "scene never muted after the line finished")
        self.assertFalse(state._sleepy_auto_muted_speaker)
        state._save_boot_sidecar.assert_any_call(state._SPEAKER_STATE_PATH, {"muted": True})

    def test_scene_then_sleepy_in_one_reply_hands_the_mute_to_sleep(self):
        """`[HW:/scene:night][HW:/emotion:sleepy] Lights down… Night, Darren.`"""
        state = self.state
        tts = FakeTTS()
        state.tts_service = tts
        tts.speak_after(delay_s=0.06, duration_s=0.25)

        self._activate("night")
        # The sleepy marker lands a few ms after the scene one.
        state._current_emotion = "sleepy"
        state._sleeping = True
        state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)
        self.assertIsNone(state._scene_drain_cancel, "scene drain must yield to sleep")

        time.sleep(0.15)
        self.assertFalse(state._speaker_muted, "line cut off by one of the drains")
        self.assertTrue(self._wait_for_mute())
        self.assertTrue(state._sleepy_auto_muted_speaker, "sleep must own the mute so wake restores it")
        self.assertEqual(tts.stop_calls, 0)

    def test_sleepy_then_scene_in_one_reply_still_lets_sleep_own_the_mute(self):
        state = self.state
        state.tts_service = FakeTTS()
        state._current_emotion = "sleepy"
        state._sleeping = True
        state._finalize_sleepy_peripherals(mute_mic=False, mute_speaker=True)
        self._activate("night")
        self.assertIsNone(state._scene_drain_cancel, "scene must not start a rival drain")
        self.assertTrue(self._wait_for_mute())
        self.assertTrue(state._sleepy_auto_muted_speaker)

    # --- the regressions it must not cause ---------------------------------

    def test_silent_scene_still_mutes_after_the_grace(self):
        state = self.state
        state.tts_service = FakeTTS()
        self._activate("movie")
        self.assertFalse(state._speaker_muted)
        self.assertTrue(self._wait_for_mute(), "silent scene never muted the speaker")

    def test_stalled_line_is_cut_at_the_cap(self):
        state = self.state
        tts = FakeTTS()
        state.tts_service = tts
        tts.speak_after(delay_s=0.02, duration_s=5.0)
        self._activate("reading")
        self.assertTrue(self._wait_for_mute(timeout=2.0), "cap never fired")
        self.assertEqual(tts.stop_calls, 1, "cap must stop the still-running TTS")

    def test_scene_off_mid_drain_leaves_the_speaker_alone(self):
        state = self.state
        state.tts_service = FakeTTS()
        self._activate("focus")
        self.scene.deactivate_scene()
        self.assertIsNone(state._scene_drain_cancel)
        time.sleep(0.4)
        self.assertFalse(state._speaker_muted, "drain committed after the scene was gone")

    def test_manual_unmute_mid_drain_is_not_re_muted(self):
        from hal.routes import music
        state = self.state
        state.tts_service = FakeTTS()
        self._activate("night")
        music.unmute_speaker()
        time.sleep(0.4)
        self.assertFalse(state._speaker_muted, "explicit unmute was overridden by the drain")

    def test_replacing_the_scene_restarts_the_drain_for_the_new_one(self):
        state = self.state
        state.tts_service = FakeTTS()
        self._activate("reading")
        first = state._scene_drain_cancel
        self._activate("movie")
        self.assertTrue(first.is_set(), "old scene's drain still pending")
        self.assertIsNot(state._scene_drain_cancel, first)
        self.assertTrue(self._wait_for_mute())

    def test_scene_with_speaker_on_cancels_a_pending_mute(self):
        state = self.state
        state.tts_service = FakeTTS()
        self._activate("night")
        self._activate("energize")
        self.assertIsNone(state._scene_drain_cancel)
        time.sleep(0.4)
        self.assertFalse(state._speaker_muted)

    def test_music_stops_at_once_while_speech_drains(self):
        state = self.state
        state.tts_service = FakeTTS()
        state.music_service = mock.Mock(playing=True, streaming=False)
        self._activate("night")
        state.music_service.stop.assert_called_once()
        self.assertFalse(state._speaker_muted)


if __name__ == "__main__":
    unittest.main()
