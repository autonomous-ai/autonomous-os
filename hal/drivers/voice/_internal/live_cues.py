"""Live turn feedback using the same HW emotion calls as regular realtime turns."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time

logger = logging.getLogger("hal.voice")


class LiveVoiceCues:
    def __init__(self, *, silence_s=0.8, timeout_s=25.0, clock=time.monotonic,
                 show=None, clear=None):
        self._executor = None
        self._pending = None
        if show is None or clear is None:
            # Emotion effects can join a hardware thread. Keep those waits off
            # the continuously streaming microphone, preserving call order.
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-emotion")
            show = self._show_emotion
            clear = self._clear_emotion
        self._show = show
        self._clear = clear
        self._clock = clock
        self._silence_s = max(0.1, silence_s)
        self._timeout_s = max(1.0, timeout_s)
        self._lock = threading.RLock()
        self._owner = object()
        self._phase = ""
        self._key = ""
        self._retired = deque(maxlen=64)
        self._last_speech = 0.0
        self._thinking_at = 0.0
        self._closed = False

    @staticmethod
    def _show_emotion(owner, phase):
        from hal.drivers.voice.voice_service import VoiceService
        from hal.drivers.voice._internal.realtime_turn import _thinking_cue_start

        if phase == "thinking":
            _thinking_cue_start()
        else:
            VoiceService._set_emotion_local("listening")

    @staticmethod
    def _clear_emotion(owner):
        from hal import app_state
        from hal.drivers.voice._internal.realtime_turn import _thinking_cue_clear

        _thinking_cue_clear()
        app_state.clear_listening_cue()

    def _paint(self, phase):
        if self._closed or phase == self._phase:
            return
        self._phase = phase

        def apply(owner=self._owner):
            try:
                if phase:
                    self._show(owner, phase)
                else:
                    self._clear(owner)
            except Exception as exc:
                logger.warning("[live] HW emotion cue failed: %s", exc)

        if self._executor is None:
            apply()
        else:
            self._pending = self._executor.submit(apply)

    def _new_input(self):
        if self._key:
            self._retired.append(self._key)
        self._key = ""
        self._paint("")
        self._owner = object()

    def speech(self):
        """Called only for already-confirmed speech, never to gate the uplink."""
        with self._lock:
            if self._closed:
                return
            now = self._clock()
            if not self._phase or now - self._last_speech > self._silence_s:
                self._new_input()
            self._last_speech = now
            self._paint("listening")

    def input(self, key, endpoint_at=None):
        with self._lock:
            if self._closed or not key or key in self._retired:
                return
            if self._key and key != self._key:
                self._new_input()
                self._last_speech = self._clock()
            self._key = key
            if endpoint_at is not None:
                if self._phase != "thinking":
                    self._thinking_at = self._clock()
                self._paint("thinking")
            elif self._phase != "thinking":
                self._last_speech = self._clock()
                self._paint("listening")

    def tick(self):
        """A silence estimate changes emotion only; the provider still ends turns."""
        with self._lock:
            now = self._clock()
            if self._phase == "listening" and now - self._last_speech >= self._silence_s:
                self._thinking_at = now
                self._paint("thinking")
            elif self._phase == "thinking" and now - self._thinking_at >= self._timeout_s:
                if self._key:
                    self._retired.append(self._key)
                self._key = ""
                self._paint("")

    def finish(self, key):
        """Old replies/terminals must not clear a newer user's cue."""
        with self._lock:
            if self._closed or key != self._key:
                return
            if key:
                self._retired.append(key)
            self._key = ""
            self._paint("")
            pending = self._pending
        # Finish before playback starts, so queued thinking cannot overwrite
        # the speaking emotion. This runs on the output pump, never the mic.
        if pending is not None:
            pending.result()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._paint("")
            self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=True)
