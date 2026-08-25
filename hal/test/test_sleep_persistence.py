"""Sleep must survive a HAL restart (an OTA restarts the service)."""
import os, sys, unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


class SleepSidecarTest(unittest.TestCase):
    def setUp(self):
        from hal import app_state as state
        self.state = state
        for p in (state._SLEEP_STATE_PATH,):
            if os.path.exists(p):
                os.unlink(p)

    def test_sleep_flag_round_trips_through_the_sidecar(self):
        state = self.state
        state._sleeping = True
        state._persist_sleep_state()
        self.assertTrue(os.path.exists(state._SLEEP_STATE_PATH))

        # Simulate the restart: forget the flag, reload from disk.
        state._sleeping = False
        state._load_peripheral_sidecars()
        self.assertTrue(state._sleeping, "device woke itself up across a restart")

    def test_waking_clears_it(self):
        state = self.state
        state._sleeping = True
        state._persist_sleep_state()
        state._sleeping = False
        state._persist_sleep_state()
        state._sleeping = True          # pretend the restart lost the truth
        state._load_peripheral_sidecars()
        self.assertFalse(state._sleeping, "restart resurrected a sleep the user ended")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SleepOwnedMutesTest(unittest.TestCase):
    """Sleep mutes the mic and speaker in memory and marks them sleep-owned, so
    waking hands them back to whatever the USER had chosen. They must survive a
    restart with that ownership intact — otherwise a sleeping device comes back
    listening, and a turn still in flight speaks out loud."""

    def setUp(self):
        from hal import app_state as state
        self.state = state
        if os.path.exists(state._SLEEP_STATE_PATH):
            os.unlink(state._SLEEP_STATE_PATH)

    def test_sleep_owned_mutes_survive_a_restart(self):
        state = self.state
        state._sleeping = True
        state._sleepy_auto_muted_mic = True
        state._sleepy_auto_muted_speaker = True
        state._persist_sleep_state()

        # Restart: every in-memory flag is gone.
        state._sleeping = False
        state._mic_muted = False
        state._speaker_muted = False
        state._sleepy_auto_muted_mic = False
        state._sleepy_auto_muted_speaker = False
        state._load_peripheral_sidecars()

        self.assertTrue(state._sleeping)
        self.assertTrue(state._mic_muted, "came back listening while asleep")
        self.assertTrue(state._speaker_muted, "came back able to speak while asleep")
        self.assertTrue(state._sleepy_auto_muted_mic, "lost sleep ownership of the mic mute")
        self.assertTrue(state._sleepy_auto_muted_speaker)

    def test_awake_restart_does_not_mute(self):
        state = self.state
        state._sleeping = False
        state._sleepy_auto_muted_mic = False
        state._sleepy_auto_muted_speaker = False
        state._persist_sleep_state()
        state._mic_muted = False
        state._speaker_muted = False
        state._load_peripheral_sidecars()
        self.assertFalse(state._sleeping)
        self.assertFalse(state._mic_muted)
        self.assertFalse(state._speaker_muted)
