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
