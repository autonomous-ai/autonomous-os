"""Hardware AEC residual-echo gate; all levels are normalized RMS.

This is an energy heuristic, not a speech classifier. Capture stays open; only
unconfirmed audio during playback/tail is replaced with silence for the provider.
Call from one audio consumer and reset when the capture stream/session changes.
"""

from collections import deque
import math

import numpy as np


class AdaptiveLiveGate:
    IDLE_FLOOR = 10 ** (-42 / 20)
    PLAYBACK_FLOOR = 10 ** (-27 / 20)
    AEC_WARMUP_S = 3.0

    def __init__(self):
        self.reset()

    def reset(self):
        self.noise = 0.003
        self.threshold = max(self.IDLE_FLOOR, self.noise * 4)
        self.speaking = False
        self.duck = False
        self.coupling_db = -8.0
        self._time = 0.0
        self._dt = 0.02
        self._tail_until = 0.0
        self._above = self._below = self._duck_quiet = 0.0
        self._envelope = deque()
        self._prefix = deque()
        self._prefix_samples = 0
        self._burst_peak = None
        self._rate = None
        self._risk = False
        self.speech_started = False
        self.barge_in = False
        self.replayed_samples = 0

    @property
    def risk(self):
        """Whether playback or its acoustic tail requires echo suppression."""
        return self._risk

    @property
    def buffered_samples(self):
        return self._prefix_samples

    @property
    def output_envelope(self):
        return max((value for _, value in self._envelope), default=0.0)

    def idle_threshold(self, rms, duration=None):
        """Track ambient only: ignore samples above the existing speech threshold.

        Like the demo, a sudden persistent noise jump above this threshold is
        deliberately not learned: this energy heuristic cannot distinguish it
        from speech. Such an environment needs better AEC or a speech classifier.
        """
        if duration is not None and (not math.isfinite(duration) or duration <= 0):
            raise ValueError("duration must be finite and positive")
        threshold = max(self.IDLE_FLOOR, self.noise * 4)
        if not self.speaking and rms <= threshold:
            # Equivalent to the demo's 20 ms fast-down/slow-up smoothing.
            retention = 0.9 if rms < self.noise else 0.995
            alpha = 1 - retention ** ((self._dt if duration is None else duration) / 0.02)
            self.noise = min(0.05, max(0.0005, self.noise + alpha * (rms - self.noise)))
        self.threshold = max(self.IDLE_FLOOR, self.noise * 4)
        return self.threshold

    def _remember(self, frame, rate):
        self._prefix.append(frame.copy())
        self._prefix_samples += frame.size
        limit = round(rate * 0.3)
        while self._prefix_samples > limit:
            excess = self._prefix_samples - limit
            first = self._prefix.popleft()
            if first.size > excess:
                self._prefix.appendleft(first[excess:])
                self._prefix_samples -= excess
            else:
                self._prefix_samples -= first.size

    def _clear_prefix(self):
        self._prefix.clear()
        self._prefix_samples = 0

    def process(self, data, rate, playback, output_level, playback_seconds=0.0):
        """Return flat mono int16 PCM, optionally prepended with withheld onset.

        Accept mono (N,) or (N, 1). Pass output RMS before duck gain, and actual
        local playback state. speech_started and barge_in are one-call pulses;
        consume barge_in once to interrupt locally, rather than testing speaking.
        Output can exceed input by at most 300 ms; send it once without refeeding.
        """
        frame = np.asarray(data)
        if frame.ndim == 2 and frame.shape[1] == 1:
            frame = frame[:, 0]
        if frame.dtype != np.int16 or frame.ndim != 1:
            raise ValueError("expected mono int16 PCM")
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate must be finite and positive")
        if not math.isfinite(output_level):
            raise ValueError("output level must be finite")
        self.speech_started = False
        self.barge_in = False
        self.replayed_samples = 0
        if frame.size == 0:
            return frame
        if self._rate is not None and rate != self._rate:
            self.reset()
        self._rate = rate
        self._dt = frame.size / rate
        self._time += self._dt
        pcm = frame.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(pcm * pcm)))
        if playback:
            self._tail_until = self._time + 0.3
        risk = bool(playback or self._time < self._tail_until)
        if risk != self._risk:
            # Do not carry an unconfirmed idle burst into a playback burst.
            self._above = 0.0
            self._burst_peak = None
            if risk:
                # Idle speech may be stale or echo at the start of playback.
                # Reconfirm against playback thresholds before opening/ducking.
                self.speaking = False
                self._below = 0.0
                self._clear_prefix()
        self._risk = risk
        level = max(0.0, float(output_level)) if playback else 0.0
        self._envelope.append((self._time, level))
        while self._envelope and self._envelope[0][0] <= self._time - 0.5:
            self._envelope.popleft()
        envelope = self.output_envelope

        if risk:
            self.threshold = max(self.PLAYBACK_FLOOR, self.noise * 5,
                                 envelope * 10 ** ((self.coupling_db + 8) / 20))
            need = 0.24
        else:
            self.threshold = self.idle_threshold(rms)
            need = 0.16
        above = rms > self.threshold
        # Like the demo, wait for speaker audio to warm up the board AEC.
        # Queue/network wait must not count. Reconfirm after warm-up so an
        # earlier echo burst cannot open the uplink or duck the speaker.
        warming_up = risk and playback_seconds < self.AEC_WARMUP_S
        if warming_up:
            self.speaking = False
            self.duck = False
            self._above = 0.0
            self._clear_prefix()
        was_speaking = self.speaking
        if risk and not self.speaking and envelope > 0.003:
            ratio = 20 * math.log10(max(rms, 1e-9) / envelope)
            if above:
                self._burst_peak = max(ratio, self._burst_peak if self._burst_peak is not None else ratio)
            else:
                if self._burst_peak is not None:
                    target = min(0.0, self._burst_peak - 8 + 2)
                    self.coupling_db += 0.7 * max(0.0, target - self.coupling_db)
                    self._burst_peak = None
                alpha = 1 - 0.95 ** (self._dt / 0.02)
                self.coupling_db += alpha * max(0.0, min(0.0, ratio) - self.coupling_db)
                self.coupling_db = max(-30.0, self.coupling_db - self._dt)
        if above and not warming_up:
            self._above += self._dt
            self._below = 0.0
        else:
            self._above = 0.0
            self._below += self._dt
        if not self.speaking and self._above + 1e-9 >= need:
            self.speaking = True
            self.speech_started = True
            self._burst_peak = None
            if risk:
                self.barge_in = True
                self.duck = True
                self._duck_quiet = 0.0
        elif self.speaking and self._below + 1e-9 >= 0.5:
            self.speaking = False
        if self.duck:
            self._duck_quiet = 0.0 if above else self._duck_quiet + self._dt
            if self._duck_quiet + 1e-9 >= 1.5 or not risk:
                self.duck = False

        if warming_up:
            return np.zeros_like(frame)
        if risk and not self.speaking:
            self._remember(frame, rate)
            return np.zeros_like(frame)
        if risk and self.speaking and not was_speaking and self._prefix:
            # Current frame has never entered prefix, so its samples appear once.
            result = np.concatenate((*self._prefix, frame))
            self.replayed_samples = self._prefix_samples
            self._clear_prefix()
            return result
        self._clear_prefix()
        return frame
