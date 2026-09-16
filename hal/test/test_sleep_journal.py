"""The device has to be able to say how often it has slept.

It could not. The sleep sidecar holds one record, every transition overwrites
it, and a reboot deletes it -- so asked "how many times have you slept", the
agent had nothing to read and did not know it had ever slept at all.

os-server's flow events were no answer either: they only record transitions
os-server itself fired, which misses both button routes entirely. So these
tests care most about the paths that leave HAL silently -- a physical sleep and
a physical wake -- and about the journal being appended to rather than
overwritten, which is the property the sidecar lacks.
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


class SleepJournalTest(unittest.TestCase):
    def setUp(self):
        from hal import app_state as state
        from hal import config
        from hal.presets import EMO_SLEEPY

        self.state = state
        self.config = config
        self.EMO_SLEEPY = EMO_SLEEPY

        self._tmp = tempfile.TemporaryDirectory()
        self._saved = {
            "dir": config.SLEEP_LOG_DIR,
            "days": config.SLEEP_LOG_MAX_DAYS,
            "emotion": state._current_emotion,
            "sleeping": state._sleeping,
            "tts": state.tts_service,
            "music": state.music_service,
            "voice": state.voice_service,
            "rgb": state.rgb_service,
            "animation": state.animation_service,
        }
        config.SLEEP_LOG_DIR = os.path.join(self._tmp.name, "sleep")
        config.SLEEP_LOG_MAX_DAYS = 30
        # The journal is the subject; the body is not. Without services the
        # route still runs its state machine and still reaches the transition.
        state.tts_service = None
        state.music_service = None
        state.voice_service = None
        state.rgb_service = None
        state.animation_service = None
        state._sleeping = False
        state._current_emotion = None

    def tearDown(self):
        state, s = self.state, self._saved
        state._cancel_sleepy_speaker_drain()
        self.config.SLEEP_LOG_DIR = s["dir"]
        self.config.SLEEP_LOG_MAX_DAYS = s["days"]
        state._current_emotion = s["emotion"]
        state._sleeping = s["sleeping"]
        state.tts_service = s["tts"]
        state.music_service = s["music"]
        state.voice_service = s["voice"]
        state.rgb_service = s["rgb"]
        state.animation_service = s["animation"]
        self._tmp.cleanup()

    def _entries(self):
        """Every journal line across every day file, in write order."""
        out = []
        if not os.path.isdir(self.config.SLEEP_LOG_DIR):
            return out
        for name in sorted(os.listdir(self.config.SLEEP_LOG_DIR)):
            with open(os.path.join(self.config.SLEEP_LOG_DIR, name)) as f:
                out.extend(json.loads(line) for line in f if line.strip())
        return out

    # --- the four routes into and out of sleep ------------------------------

    def test_an_agent_marker_is_recorded(self):
        """[HW:/emotion:sleepy] -- the one route that already had a record."""
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        express_emotion(EmotionRequest(emotion=self.EMO_SLEEPY))

        entries = self._entries()
        self.assertEqual(len(entries), 1, "the marker sleep was not journalled")
        self.assertEqual(entries[0]["event"], "sleep")
        self.assertEqual(entries[0]["source"], "api")

    def test_a_button_sleep_is_recorded(self):
        """The route os-server never sees, so the journal is its only record."""
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        express_emotion(EmotionRequest(emotion=self.EMO_SLEEPY), source="button")

        entries = self._entries()
        self.assertEqual(len(entries), 1, "a physical sleep went unrecorded")
        self.assertEqual(entries[0]["event"], "sleep")
        self.assertEqual(entries[0]["source"], "button",
                         "a physical sleep was filed as if the agent had asked")

    def test_a_button_wake_is_recorded(self):
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        express_emotion(EmotionRequest(emotion=self.EMO_SLEEPY), source="button")
        express_emotion(EmotionRequest(emotion="stretching"), source="touch")

        entries = self._entries()
        self.assertEqual([e["event"] for e in entries], ["sleep", "wake"])
        self.assertEqual(entries[1]["source"], "touch")
        self.assertEqual(entries[1]["emotion"], "stretching")

    def test_a_greeting_wake_is_recorded(self):
        """presence.enter reaches the agent even while asleep, and greeting is
        one of the three emotions the sleep gate lets through."""
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        express_emotion(EmotionRequest(emotion=self.EMO_SLEEPY))
        express_emotion(EmotionRequest(emotion="greeting"))

        entries = self._entries()
        self.assertEqual([e["event"] for e in entries], ["sleep", "wake"])
        self.assertEqual(entries[1]["emotion"], "greeting")

    # --- what the sidecar could not do -------------------------------------

    def test_repeated_sleeps_accumulate_instead_of_overwriting(self):
        """The whole point. The sidecar keeps one record; this keeps the count."""
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        for _ in range(3):
            express_emotion(EmotionRequest(emotion=self.EMO_SLEEPY), source="button")
            express_emotion(EmotionRequest(emotion="stretching"), source="button")

        entries = self._entries()
        slept = [e for e in entries if e["event"] == "sleep"]
        self.assertEqual(len(slept), 3, "sleeps overwrote each other -- nothing to count")
        self.assertEqual(len(entries), 6)

    def test_a_repeat_sleepy_is_not_a_second_sleep(self):
        """presence.away and the night scene re-send sleepy to an already
        sleeping device. Counting those would inflate the total."""
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        express_emotion(EmotionRequest(emotion=self.EMO_SLEEPY))
        express_emotion(EmotionRequest(emotion=self.EMO_SLEEPY))
        express_emotion(EmotionRequest(emotion=self.EMO_SLEEPY))

        self.assertEqual(len(self._entries()), 1,
                         "a re-sent sleepy was counted as falling asleep again")

    def test_each_entry_carries_the_time_of_day(self):
        """"How many times" is the easy half; "when do I usually sleep" needs
        the clock, and the sidecar carried no timestamp at all."""
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        before = time.time()
        express_emotion(EmotionRequest(emotion=self.EMO_SLEEPY))
        entry = self._entries()[0]

        self.assertGreaterEqual(entry["ts"], int(before))
        self.assertIn("date", entry)
        self.assertIsInstance(entry["hour"], int)
        self.assertTrue(0 <= entry["hour"] <= 23)

    # --- the journal must never cost the device its sleep -------------------

    def test_an_unwritable_journal_does_not_block_sleep(self):
        """A record of sleep is worth less than the sleep itself."""
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        # A path that cannot be created: a file where the directory must go.
        blocked = os.path.join(self._tmp.name, "not-a-dir")
        with open(blocked, "w") as f:
            f.write("")
        self.config.SLEEP_LOG_DIR = os.path.join(blocked, "sleep")

        express_emotion(EmotionRequest(emotion=self.EMO_SLEEPY))

        self.assertTrue(self.state._sleeping, "a failed journal write blocked sleep")

    # --- retention ----------------------------------------------------------

    def test_days_past_the_window_are_pruned(self):
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        os.makedirs(self.config.SLEEP_LOG_DIR, exist_ok=True)
        stale = os.path.join(self.config.SLEEP_LOG_DIR, "2020-01-01.jsonl")
        fresh = os.path.join(self.config.SLEEP_LOG_DIR, "2020-01-02.jsonl")
        for path in (stale, fresh):
            with open(path, "w") as f:
                f.write('{"event": "sleep"}\n')
        old = time.time() - (self.config.SLEEP_LOG_MAX_DAYS + 1) * 86400
        os.utime(stale, (old, old))

        express_emotion(EmotionRequest(emotion=self.EMO_SLEEPY))

        self.assertFalse(os.path.exists(stale), "a day past the window was kept")
        self.assertTrue(os.path.exists(fresh), "a day inside the window was pruned")


if __name__ == "__main__":
    unittest.main(verbosity=2)
