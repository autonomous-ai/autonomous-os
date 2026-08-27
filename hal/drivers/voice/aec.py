"""Acoustic echo cancellation for the mic path (WebRTC AEC3).

Provider-independent: the reference is tapped at the TTS output stream, so it
covers synthesized speech, queued speech and realtime native audio alike.

Requires `aec-audio-processing` (SWIG binding over libwebrtc-audio-processing-2).
It is NOT a hal dependency — absent or unloadable, every entry point here
degrades to a no-op and the voice path behaves exactly as before.

Wiring:
    tts/service.py  _WatchedStream.write  → reference_write()
    voice_service.py mic open             → wrap_mic()
"""

import logging
import os
import threading
import time
from math import gcd
from typing import Optional

logger = logging.getLogger("hal.voice.aec")

FRAME_MS = 10  # APM's fixed frame size
# Mean square of an int16 frame at RMS 1000 — well over the ~6 room floor
# measured on this hardware, so a 'dry and loud' frame really is the mic
# hearing the speaker with no reference to cancel it against.
_DRY_LOUD_MSQ = 1000.0 ** 2
SUPPORTED_RATES = (8000, 16000, 32000, 48000)
# Envelope test resolution. 8ms resolves syllable onsets (a syllable runs
# 80-250ms) while keeping the lag search over ~800ms of reference to well under
# a hundred dot products, so it costs nothing on the candidate path.
_ENVELOPE_HOP_MS = 8
# Refuse to score unless the reference is long enough to actually SEARCH: with
# no slack the comparison degenerates to a single fixed alignment, which is the
# assumption that drifting USB clocks already broke.
_ENVELOPE_MIN_LAGS = 8
# Retention for both the reference and the raw-mic history the envelope test
# reads. Not just candidate window (~384ms) + speaker->mic delay (~205ms): the
# TTS tap fires when ALSA ACCEPTS audio and runs AHEAD of playback in
# network-paced bursts, so the reference matching a given mic window can be far
# older than the delay suggests. At 800ms the search ran off the end and pinned
# lag=0 — device-observed 27/08/2026, a misaligned fit read 3.8dB of excess and
# cut the reply off. 2s of int16 mono is 64KB; the extra lags are dot products
# over ~48 points.
_HISTORY_MS = 2000
# Excess (in dB) at which the score reaches 0.5 — i.e. how much unexplained
# energy counts as "someone else is in this window". Sets the sensitivity of the
# residual test; lower = quicker to call it a person.
_EXCESS_HALF_DB = 3.0
# Skew below which the window is echo outright. Labelled measurement on
# lamp-0c89 27/08/2026, speaker 40%, ~40 echo windows against interruptions
# confirmed by the transcript that followed:
#   echo    -50.0 .. +4.8 dB   (all but one at or below +1.3)
#   person   +8.4 .. +40.4 dB
# 5.0 clears the single 4.8 outlier. Combined with _EXCESS_HALF_DB the
# effective cut lands near 6.6dB, i.e. inside the gap and biased towards
# missing a quiet interruption over cutting the reply off.
_SKEW_ECHO_FLOOR_DB = 5.0
# Correlation at or above which the window tracks the reply closely enough to
# be echo — but only together with the skew ceiling below, never on its own.
# Highest confirmed real interruption measured 0.88.
_CORR_CERTAIN_ECHO = 0.90
# ...and only while the unexplained energy stays under this. A person loud
# enough to add 10dB the reference cannot account for is a person even if their
# rhythm happens to follow the reply; every confirmed interruption that came
# with a high correlation sat far above it.
_SKEW_VETO_CEILING_DB = 10.0
# How far above the measured speaker->mic coupling a window has to sit before
# level alone convicts it. The skew test cannot catch a person who covers the
# WHOLE window — subtracting the median then removes them with the echo, which
# is how an RMS=20452 interruption scored 0.9dB of skew and was rejected
# (device-observed 27/08/2026, against an echo ceiling near 10000).
# Coupling is learned from echo-only windows rather than declared, so this stays
# a ratio and needs no per-body value — the weakness that rules out a plain
# Geigel detector, whose fixed threshold depends on the echo path attenuation.
_COUPLING_EXCESS_DB = 6.0
# Slow EMA: one loud window must not drag the baseline up to meet itself.
_COUPLING_EMA = 0.05
_coupling_db = None
# How far below the window's loudest reference frame still counts as "the reply
# is playing here". 15dB spans a sentence's dynamics without reaching into the
# silences between its words.
_LOUD_WINDOW_DB = 15.0

_lock = threading.Lock()
_canceller = None
_reference = None
_unavailable_logged = False


class EchoReference:
    """FIFO of audio handed to the speaker, drained by the canceller.

    Bounded to `max_ms`: older audio is past any useful alignment and letting it
    pile up drifts the reference out of step with the mic.
    """

    def __init__(self, rate: int, max_ms: int = 500, history_ms: int = 800):
        self._max_ms = max_ms
        self._rate = rate
        self._buffer = bytearray()
        self._max_bytes = int(rate * 2 * max_ms / 1000)
        # Separate from the FIFO above, which process() DRAINS: by the time a
        # barge-in candidate is judged, the reference for the frames it is made
        # of is long gone. This keeps a copy that nothing drains, so the
        # envelope test can still look back at what the speaker was playing.
        # 800ms covers the candidate window plus the speaker->mic delay plus
        # the lag search on top.
        self._history = bytearray()
        self._history_max = int(rate * 2 * history_ms / 1000)
        self._lock = threading.Lock()
        self._last_write = 0.0

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self._buffer.extend(pcm)
            if len(self._buffer) > self._max_bytes:
                del self._buffer[: len(self._buffer) - self._max_bytes]
            self._history.extend(pcm)
            if len(self._history) > self._history_max:
                del self._history[: len(self._history) - self._history_max]
            self._last_write = time.monotonic()

    def history(self) -> bytes:
        """Everything recently handed to the speaker, oldest first."""
        with self._lock:
            return bytes(self._history)

    def read(self, nbytes: int):
        """Take the next `nbytes` of played audio; zero-pad if it ran dry.

        Returns `(pcm, underran)`. The flag matters: an all-zero reference is
        ambiguous on its own — it means either a genuine silence in the reply
        (nothing to cancel, mic is clean) or a starved FIFO (echo present, no
        reference to cancel it with). Only the caller that knows which can make
        a safe decision, so say it here instead of guessing downstream.
        """
        with self._lock:
            if len(self._buffer) >= nbytes:
                out = bytes(self._buffer[:nbytes])
                del self._buffer[:nbytes]
                return out, False
            out = bytes(self._buffer) + b"\x00" * (nbytes - len(self._buffer))
            self._buffer.clear()
            return out, True

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._history.clear()

    def idle_for(self) -> float:
        """Seconds since the last speaker write (inf if nothing was ever played)."""
        with self._lock:
            if self._last_write <= 0.0:
                return float("inf")
            return time.monotonic() - self._last_write


class EchoCanceller:
    """Runs mic audio through WebRTC's APM with played audio as the reference.

    `process()` is the only hot path: it buffers to APM's fixed 10 ms frames and
    returns exactly as many samples as the caller asked for, so it drops into an
    existing read loop without changing framing.
    """

    def __init__(self, rate: int, reference: EchoReference, delay_ms: int,
                 noise_suppression: bool, dump_dir: str = ""):
        self._rate = rate
        self._reference = reference
        self._frame_bytes = int(rate * FRAME_MS / 1000) * 2
        self._pending = bytearray()
        self._out = bytearray()
        self._apm = None
        self._dump = None
        self._erle_log_at = 0.0
        self._erle_mic = 0.0
        self._erle_out = 0.0
        self._erle_frames = 0
        self._erle_dry = 0
        self._erle_dry_loud = 0
        # True when the mic frame just returned was NOT fully cancelled, so
        # callers can refuse to act on it. See uncancelled().
        self._uncancelled = True
        # RAW mic, before the APM touches it, for echo_envelope_match(). It has
        # to be the raw signal: cancellation is a time-varying gain, so it eats
        # exactly the loudness contour the envelope test compares. Measured
        # 27/08/2026 on post-APM audio, echo scored 0.42 / 0.45 / 0.81 / 0.97 —
        # the two low scores are the frames the APM cancelled WELL, and they are
        # the ones that leaked through and cut the reply off.
        self._mic_history = bytearray()
        self._mic_history_max = int(rate * 2 * _HISTORY_MS / 1000)
        # The same window AFTER the APM. Held alongside the raw copy so the two
        # can be compared over exactly the candidate window: the APM cancels
        # what is in the reference and cannot touch what is not, so echo comes
        # out suppressed and a person comes out untouched. That ratio is a
        # discriminator the envelope shape cannot provide.
        self._clean_history = bytearray()

        from aec_audio_processing import AudioProcessor

        self._apm = AudioProcessor(
            enable_aec=True,
            enable_ns=noise_suppression,
            enable_agc=False,  # AGC rides the gain up under the bot's own voice
            enable_vad=False,  # hal runs webrtcvad/silero/ten-vad for that
        )
        self._apm.set_stream_format(rate, 1)
        self._apm.set_reverse_stream_format(rate, 1)
        self._apm.set_stream_delay(delay_ms)
        logger.info(
            "AEC3 active: %dHz, %dms frames, delay hint %dms, ns=%s, reference %dms",
            rate, FRAME_MS, delay_ms, noise_suppression, reference._max_ms,
        )
        if dump_dir:
            self._open_dump(dump_dir, rate)

    def reset(self) -> None:
        """Drop buffered audio after a route change or stream reopen."""
        self._pending.clear()
        self._out.clear()
        self._mic_history.clear()
        self._clean_history.clear()
        self._reference.clear()

    def process(self, pcm: bytes) -> bytes:
        """Cancel the speaker signal out of `pcm`, returning the same byte count.

        Primes with up to one 10 ms frame of silence on the first call; from then
        on input and output stay length-for-length.
        """
        want = len(pcm)
        self._mic_history.extend(pcm)
        if len(self._mic_history) > self._mic_history_max:
            del self._mic_history[: len(self._mic_history) - self._mic_history_max]
        self._pending.extend(pcm)
        uncancelled = False
        while len(self._pending) >= self._frame_bytes:
            mic = bytes(self._pending[: self._frame_bytes])
            del self._pending[: self._frame_bytes]
            played, underran = self._reference.read(self._frame_bytes)
            uncancelled = uncancelled or underran
            self._apm.process_reverse_stream(played)
            cleaned = self._apm.process_stream(mic)
            self._out.extend(cleaned)
            self._clean_history.extend(cleaned)
            if len(self._clean_history) > self._mic_history_max:
                del self._clean_history[
                    : len(self._clean_history) - self._mic_history_max
                ]
            self._accumulate_erle(mic, cleaned, played, underran)
            if self._dump:
                self._dump["mic"].writeframes(mic)
                self._dump["ref"].writeframes(played)
                self._dump["out"].writeframes(cleaned)
        self._uncancelled = uncancelled
        if len(self._out) < want:
            self._out[:0] = b"\x00" * (want - len(self._out))
        out = bytes(self._out[:want])
        del self._out[:want]
        return out

    def close(self) -> None:
        self._apm = None
        if self._dump:
            for handle in self._dump.values():
                handle.close()
            self._dump = None

    def _accumulate_erle(
        self, mic: bytes, cleaned: bytes, played: bytes, underran: bool
    ) -> None:
        """Log echo return loss enhancement while the speaker is actually active.

        This is the number that says whether the canceller is doing anything:
        0 dB means it is not.

        Frames whose reference came back all-zero are counted SEPARATELY rather
        than skipped. The reference is a FIFO drained by the mic loop, so it can
        run dry while the speaker is still playing — after an arecord overrun
        (AecStream returns early without draining it), or whenever the tap falls
        out of step with playback. Those frames get no cancellation at all, and
        skipping them silently meant ERLE only ever reported the frames that
        happened to line up: a mostly-dry run logged a healthy-looking number,
        or nothing at all when no frame aligned. `dry` with a loud mic is the
        echo leak, stated directly.
        """
        import numpy as np

        m = np.frombuffer(mic, dtype=np.int16).astype(np.float32)
        m_msq = float(np.mean(m ** 2))
        if underran or not any(played):
            if underran:
                self._erle_dry += 1
                if m_msq > _DRY_LOUD_MSQ:
                    self._erle_dry_loud += 1
        else:
            c = np.frombuffer(cleaned, dtype=np.int16).astype(np.float32)
            self._erle_mic += m_msq
            self._erle_out += float(np.mean(c ** 2))
            self._erle_frames += 1
        now = time.monotonic()
        # Flush on total frames, not cancelled frames: an all-dry window has
        # _erle_frames == 0 forever and would never report itself.
        if (self._erle_frames + self._erle_dry) >= 100 and now - self._erle_log_at > 2.0:
            if self._erle_out > 0 and self._erle_mic > 0:
                import math

                erle = "%.1f dB" % (10 * math.log10(self._erle_mic / self._erle_out))
            else:
                erle = "n/a"
            logger.info(
                "AEC ERLE %s over %d cancelled frames; reference UNDERRAN on %d "
                "frames (%d of them with a loud mic = uncancelled echo)",
                erle,
                self._erle_frames,
                self._erle_dry,
                self._erle_dry_loud,
            )
            self._erle_log_at = now
            self._erle_mic = self._erle_out = 0.0
            self._erle_frames = 0
            self._erle_dry = self._erle_dry_loud = 0

    def _open_dump(self, dump_dir: str, rate: int) -> None:
        import wave

        os.makedirs(dump_dir, exist_ok=True)
        self._dump = {}
        for name in ("mic", "ref", "out"):
            handle = wave.open(os.path.join(dump_dir, f"aec_{name}.wav"), "wb")
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            self._dump[name] = handle
        logger.info("AEC dumping mic/ref/out wavs to %s", dump_dir)


class AecStream:
    """Mic stream wrapper that cancels echo out of every read.

    Wraps any object with the `sd.InputStream` read contract (also satisfied by
    ArecordStream): `read(frames) -> (int16 ndarray, overflowed)`.
    """

    def __init__(self, inner, canceller: EchoCanceller, tail_s: float, np):
        self._inner = inner
        self._canceller = canceller
        self._tail_s = tail_s
        self._np = np
        self._bypassed = True

    def __enter__(self):
        self._inner = self._inner.__enter__()
        self._canceller.reset()
        return self

    def __exit__(self, *args):
        return self._inner.__exit__(*args)

    def read(self, frames):
        data, overflowed = self._inner.read(frames)
        if overflowed:
            if self._canceller is not None:
                self._canceller._uncancelled = True
            return data, overflowed
        # Skip the APM entirely when nothing has played recently — echo only
        # exists near playback, and this is a 24/7 loop on an 8-core A55.
        idle = self._canceller._reference.idle_for()
        if idle > self._tail_s:
            if not self._bypassed:
                # Logged because `active()` keeps returning True here: callers
                # that relax an echo defence (barge-in) stay armed while the mic
                # is running raw. If this fires while TTS is still speaking, the
                # reference tap stopped early and the leak is downstream of it.
                logger.info(
                    "AEC bypassed — no speaker write for %.1fs (tail %.1fs)",
                    idle, self._tail_s,
                )
                self._canceller.reset()
                self._bypassed = True
            self._canceller._uncancelled = True
            return data, overflowed
        if self._bypassed:
            logger.info("AEC engaged — speaker active again")
        self._bypassed = False
        try:
            cleaned = self._canceller.process(data.tobytes())
        except Exception as e:
            # Mark the frame raw before handing it back. The bypass branch above
            # does this; forgetting it here left `_uncancelled` holding whatever
            # the PREVIOUS frame set, so a failure right after a successful frame
            # published full speaker bleed as "cancelled" — and barge-in, whose
            # only echo defence is that flag, would interrupt the device on its
            # own voice.
            self._canceller._uncancelled = True
            logger.warning("AEC process failed, passing mic through: %s", e)
            return data, overflowed
        return (
            self._np.frombuffer(cleaned, dtype=self._np.int16).reshape(frames, -1),
            overflowed,
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


def configure(rate: int) -> bool:
    """Build the canceller for a mic sample rate. Safe to call repeatedly.

    Returns False when AEC is off, the rate is unsupported, or the native
    binding is missing — callers then use the raw mic.
    """
    global _canceller, _reference, _unavailable_logged

    from hal.drivers.voice._internal import config as voice_cfg

    if not voice_cfg.AEC_ENABLED:
        return False
    if rate not in SUPPORTED_RATES:
        logger.warning("AEC disabled: %dHz not supported by the APM %s", rate, SUPPORTED_RATES)
        return False
    with _lock:
        if _canceller is not None and _canceller._rate == rate:
            return True
        if _canceller is not None:
            _canceller.close()
            _canceller = None
        try:
            _reference = EchoReference(rate, voice_cfg.AEC_REF_MS)
            _canceller = EchoCanceller(
                rate,
                _reference,
                voice_cfg.AEC_DELAY_MS,
                voice_cfg.AEC_NOISE_SUPPRESSION,
                voice_cfg.AEC_DUMP_DIR,
            )
        except Exception as e:
            _reference = None
            _canceller = None
            if not _unavailable_logged:
                _unavailable_logged = True
                logger.warning(
                    "AEC unavailable (%s) — mic runs uncancelled. Install with: "
                    "uv pip install aec-audio-processing",
                    e,
                )
            return False
    return True


def active() -> bool:
    """Whether cancellation is actually running — configured AND binding present.

    Callers that relax an echo defence must gate on this, not on AEC_ENABLED:
    with the binding missing every entry point is a no-op and the mic still
    carries full speaker bleed.
    """
    return _canceller is not None


def uncancelled() -> bool:
    """Whether the mic frame just read went through WITHOUT real cancellation.

    True when the reference FIFO underran, the stream was bypassed, or the mic
    overran — in all three the frame still carries full speaker bleed. Any
    defence that a loudspeaker can trip (barge-in) must skip such a frame, or it
    is deciding on echo. Measured on lamp-ee17 25/08/2026: the reference
    underran on 86% of processed frames during a reply, and the lamp interrupted
    itself and transcribed its own sentence as the user's.

    A genuinely SILENT reference is not uncancelled — a pause in the reply is
    exactly when a real interruption arrives cleanly.
    """
    return _canceller is None or _canceller._uncancelled


def _dump_candidate(mic_pcm, ref_bytes, corr, lag, skew_db, offset_db,
                    supp_db, np) -> None:
    """Archive one barge-in candidate for offline work. Off unless configured.

    Exists because tuning this live does not converge: each fix is fitted to the
    last failure seen, and the next batch of data moves the boundary again
    (echo skew read 4.8dB, then 6.5, then 7.8, then 64.6 across four batches on
    the same device and the same day). A labelled archive lets a rule be scored
    against every window at once, and lets features beyond these two be tried
    without another round trip to the device.

    Set HAL_BARGE_IN_DUMP_DIR to collect, and HAL_BARGE_IN_DUMP_LABEL to say
    what the batch is — run one batch into a silent room ('echo') and one with
    someone deliberately interrupting ('person').
    """
    dump_dir = os.environ.get("HAL_BARGE_IN_DUMP_DIR", "")
    if not dump_dir:
        return
    try:
        os.makedirs(dump_dir, exist_ok=True)
        label = os.environ.get("HAL_BARGE_IN_DUMP_LABEL", "unlabelled")
        # WALL clock, so a sample can be matched against the journal line that
        # followed it. The batch label alone is not trustworthy: while
        # collecting 'person' the device still interrupts itself, and every
        # such window would be filed under the wrong label. The transcript that
        # came out of the capture is the real label.
        stamp = "%.3f" % time.time()
        np.savez_compressed(
            os.path.join(dump_dir, f"{label}_{stamp}.npz"),
            mic=np.frombuffer(mic_pcm, dtype=np.int16),
            ref=np.frombuffer(ref_bytes, dtype=np.int16),
            rate=_canceller._rate if _canceller else 0,
            corr=corr, lag_hops=lag, skew_db=skew_db,
            offset_db=offset_db, supp_db=supp_db,
            label=label,
        )
    except Exception as e:  # never break the voice loop to collect data
        logger.debug("barge-in dump skipped: %s", e)


def echo_envelope_match(window_ms: int, np) -> Optional[float]:
    """How well the last `window_ms` of RAW mic tracks the reply: -1..1 or None.

    Reads the mic from the canceller's own pre-APM history rather than taking it
    from the caller, because the caller only ever holds CANCELLED audio and
    cancellation destroys what this measures (see EchoCanceller._mic_history).

    Cancelling echo needs the reference aligned to within a fraction of a
    wavelength, which the drifting clocks of a separate USB mic and speaker make
    impossible to hold — measured 26/08/2026, the delay read 230/170/55ms on
    three passes and ERLE never rose above 5.8 dB. RECOGNISING echo needs no
    such alignment: the loudness contour survives the room, the speaker and a
    half-failed cancellation, so it can simply be searched for across the whole
    plausible lag range.

    Compares log-energy envelopes at 8ms resolution, zero-meaned and normalised,
    so it answers "does this rise and fall WITH the reply" rather than "is this
    loud" — which is the distinction the level gate provably cannot make: at
    speaker 40% the echo ceiling measured 9969 while real interruptions measured
    6956-8027, so the populations overlap at every volume tested (25/40/65%).

    None when there is nothing to compare against — no canceller, or too little
    reference retained to search. None is NOT "clean": it means unknown, and the
    caller must not read it as permission to fire.
    """
    if _canceller is None or _reference is None:
        return None
    ref_bytes = _reference.history()
    want = int(_canceller._rate * 2 * window_ms / 1000)
    mic_pcm = bytes(_canceller._mic_history[-want:])
    if not ref_bytes or not mic_pcm:
        return None
    hop = int(_canceller._rate * _ENVELOPE_HOP_MS / 1000)
    if hop <= 0:
        return None

    def envelope(pcm: bytes):
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        usable = (len(a) // hop) * hop
        if usable < hop:
            return None
        frames = a[:usable].reshape(-1, hop)
        # Log domain: echo arrives 20-40 dB down, so a linear envelope would be
        # dominated by whichever signal is louder instead of by their shape.
        return np.log10(np.mean(frames ** 2, axis=1) + 1.0)

    mic_env = envelope(mic_pcm)
    ref_env = envelope(ref_bytes)
    if mic_env is None or ref_env is None:
        return None
    if len(ref_env) < len(mic_env) + _ENVELOPE_MIN_LAGS:
        return None

    centred = mic_env - mic_env.mean()
    norm = float(np.linalg.norm(centred))
    if norm <= 0.0:
        return None
    centred = centred / norm

    # Step 1 — find WHERE in the reference this window came from. Correlation
    # only picks the lag here; it is not the verdict, because during double talk
    # the raw mic carries the echo 20-30dB above the person and correlates
    # highly no matter what they say (device-observed 27/08/2026: a real
    # interruption scored 0.75-0.88 and was rejected).
    best, best_lag = -2.0, None
    for lag in range(len(ref_env) - len(mic_env) + 1):
        window = ref_env[lag: lag + len(mic_env)]
        window = window - window.mean()
        w_norm = float(np.linalg.norm(window))
        if w_norm <= 0.0:
            continue
        score = float(np.dot(centred, window / w_norm))
        if score > best:
            best, best_lag = score, lag
    if best_lag is None:
        return None
    # A fit pinned at the OLDEST edge means the true alignment fell off the far
    # end of the retained reference, so the residual below would be measuring
    # misalignment rather than a person — device-observed 27/08/2026, exactly
    # how this cut a reply off. The newest edge is not suspect in the same way:
    # it is the ordinary answer when the mic has caught up with playback.
    if best_lag == 0:
        logger.info(
            "echo envelope: lag pinned at %dms — alignment outside the retained "
            "reference, no verdict", best_lag * _ENVELOPE_HOP_MS,
        )
        return None

    # Step 2 — the verdict: is the mic FULLY EXPLAINED by the reference?
    # Subtract the aligned reference plus the coupling gain (median offset, so
    # a few loud frames cannot drag the fit). Pure echo leaves residual noise
    # around zero. A person adds energy the reference cannot account for, and
    # they show up there even while quieter than the echo — which is exactly the
    # case correlation gets wrong.
    aligned = ref_env[best_lag: best_lag + len(mic_env)]
    # Only where the reply is actually LOUD, measured against the loudest frame
    # in the window rather than its median. In the gaps between its words the
    # reference predicts near-silence, so ordinary room noise reads as a huge
    # unexplained excess and every echo would score as a person; those frames
    # also carry no information, since there is no echo there to explain.
    # A median split is not enough — most of the retained reference IS silence,
    # so the median sits at the noise floor and lets the silent frames back in.
    # Device-observed 27/08/2026: that put the learned coupling at +34.4dB,
    # i.e. the mic louder than the speaker, which disabled the level check
    # entirely and let a clear interruption through as echo.
    loud_ref = aligned >= (aligned.max() - _LOUD_WINDOW_DB / 10.0)
    if int(loud_ref.sum()) < _ENVELOPE_MIN_LAGS:
        return None
    # The median over reference-loud frames IS the speaker->mic coupling: how
    # far the mic sits below what was played. Echo holds it steady; a person
    # raises it. Estimated here and only here, because it needs frames where the
    # echo is the thing being measured.
    offset = float(np.median((mic_env - aligned)[loud_ref]))
    offset_db = offset * 10.0
    # The skew, though, is measured wherever the MIC has content — not only
    # where the reply is loud. Restricting it to reference-loud frames threw
    # away the very frames an interrupting voice occupies when they talk over a
    # quiet part of the reply, and the person vanished with them.
    judge = mic_env >= (mic_env.max() - _LOUD_WINDOW_DB / 10.0)
    if int(judge.sum()) < _ENVELOPE_MIN_LAGS:
        return None
    residual = (mic_env - aligned - offset)[judge]
    # SKEW, not size. Echo never fits perfectly — room reverb, mic noise and a
    # coupling that is not a clean scaling leave a couple of dB of residual
    # either way, and measuring its size alone read 2.1dB on pure echo and cut
    # the reply off (device-observed 27/08/2026, corr=0.96 lag=240ms).
    # What a PERSON does is one-sided: they can only ADD energy, never remove
    # it. So symmetric residual = echo however big it is, and a top tail that
    # outruns the bottom tail = something else is in the room.
    # x10: these are log10 POWER envelopes, so one unit is 10 dB.
    hi = float(np.percentile(residual, 90))
    lo = float(np.percentile(residual, 10))
    excess_db = (hi + lo) * 10.0

    # Map excess back onto the existing "1 = echo, 0 = person" scale so the
    # threshold keeps its meaning and direction. 0dB excess -> 1.0.
    # How hard the APM pushed this window down — the textbook double-talk
    # decision variable (sigma_e/sigma_d). LOGGED ONLY, deliberately: measured
    # on this hardware it does NOT separate the two (echo 0.3-10.1dB, person
    # 0.1-8.2dB, fully overlapping) because ERLE here is 6dB at best and swings
    # per frame. Kept in the log so the next person can see it was tried.
    clean = np.frombuffer(
        bytes(_canceller._clean_history[-want:]), dtype=np.int16
    ).astype(np.float32)
    raw = np.frombuffer(mic_pcm, dtype=np.int16).astype(np.float32)
    n = min(len(clean), len(raw))
    suppression_db = 0.0
    if n:
        raw_p = float(np.mean(raw[-n:] ** 2)) + 1.0
        clean_p = float(np.mean(clean[-n:] ** 2)) + 1.0
        suppression_db = 10.0 * np.log10(raw_p / clean_p)

    _dump_candidate(mic_pcm, ref_bytes, best, best_lag, excess_db,
                    offset_db, suppression_db, np)

    score = 1.0 / (
        1.0 + max(0.0, excess_db - _SKEW_ECHO_FLOOR_DB) / _EXCESS_HALF_DB
    )
    # Joint veto: a window that both TRACKS the reply closely and adds little
    # unexplained energy is the reply, whatever the residual mapping said.
    # Neither half convicts alone — that was tried and both overlap:
    #   echo skew reached 7.8dB (it was 4.8 in the first labelled batch, then
    #   6.5, then 7.8), which is inside the range real interruptions occupy;
    #   echo correlation reached 0.92 while a real interruption reached 0.88.
    # Together they separate cleanly on everything measured 27/08/2026:
    #   echo      corr 0.92 / skew 7.8   corr 0.96 / skew 2.1   corr 0.85 / 5.6
    #   person    corr 0.45 / skew 54.8  corr 0.34 / 23.3       corr 0.62 / 25.1
    # The case this exists for cut 'They won four two on aggregate' mid-word and
    # captured the lamp's own 'or two on' as the user's turn.
    if best >= _CORR_CERTAIN_ECHO and excess_db < _SKEW_VETO_CEILING_DB:
        score = 1.0

    # Coupling is MEASURED and logged but does NOT convict. It was added to
    # catch the person the skew test structurally cannot see — one who fills the
    # whole window, so subtracting the median removes them with the echo — on
    # the strength of a single observation (RMS=20452, skew 0.9dB). Over the
    # next 44 judged windows it overrode the skew verdict exactly once, and that
    # once was wrong: skew=2.4dB (echo range) but 7.9dB over the baseline, so it
    # cut the reply off and the capture came back '(empty)'. Echo's own coupling
    # swings by more than the threshold as the reply gets louder and quieter.
    # Left in the log because the blind spot it was aimed at is real; re-enable
    # only against labelled data, the way the skew threshold was set.
    global _coupling_db
    over_db = 0.0 if _coupling_db is None else offset_db - _coupling_db
    if score >= 1.0 or _coupling_db is None:
        # Learn only from windows this call is calling echo, so a run of
        # interruptions cannot teach the baseline to accept them.
        _coupling_db = (
            offset_db
            if _coupling_db is None
            else (1 - _COUPLING_EMA) * _coupling_db + _COUPLING_EMA * offset_db
        )
    logger.info(
        "echo envelope: corr=%.2f lag=%dms skew=%.1fdB coupling=%.1f/%.1fdB "
        "(over %.1f) supp=%.1fdB -> %.2f",
        best, best_lag * _ENVELOPE_HOP_MS, excess_db,
        offset_db, -99.0 if _coupling_db is None else _coupling_db, over_db,
        suppression_db, score,
    )
    return score


def raw_tail(frames: int, samples_per_frame: int, np):
    """The last `frames` mic frames as they arrived, BEFORE the APM.

    For barge-in capture. The APM does not merely attenuate the near-end talker
    while the speaker is active — it removes them: measured 27/08/2026 on
    lamp-0c89, a frame reading 7426 on the raw mic came out of the APM at 5,
    with the reference loud at 5968. STT is then handed something with the
    energy of silence and returns no transcript at all, not even a wrong one
    (device-observed: zero partials, `transcript='(empty)'`).

    The raw frames carry the reply's echo as well as the user, which is why the
    transcript-level defence exists (session_finalize.strip_echo_prefix): the
    device's own words can be dropped afterwards, the user's cannot be
    recovered once the APM has deleted them.

    Returns None when the history is short — the caller keeps what it had.
    """
    if _canceller is None:
        return None
    want = frames * samples_per_frame * 2
    history = _canceller._mic_history
    if len(history) < want or want <= 0:
        return None
    block = np.frombuffer(bytes(history[-want:]), dtype=np.int16)
    return [block[i * samples_per_frame: (i + 1) * samples_per_frame]
            for i in range(frames)]


def wrap_mic(mic_ctx, rate: int, np):
    """Return `mic_ctx` wrapped with echo cancellation, or unchanged when off."""
    if not configure(rate):
        return mic_ctx
    from hal.drivers.voice._internal import config as voice_cfg

    return AecStream(mic_ctx, _canceller, voice_cfg.AEC_TAIL_S, np)


def reference_write(samples, rate: int) -> None:
    """Record float32 audio on its way to the speaker. Never raises.

    Called from the TTS output stream write, i.e. at playback rate — which is
    the timing the mic sees. Tapping at synthesis instead would be wrong: TTS
    renders a sentence far faster than real time.
    """
    ref = _reference
    if ref is None or _canceller is None:
        return
    try:
        import numpy as np

        mono = np.asarray(samples, dtype=np.float32).reshape(-1)
        dst = _canceller._rate
        if rate != dst:
            import scipy.signal

            g = gcd(dst, rate)
            mono = scipy.signal.resample_poly(mono, dst // g, rate // g)
        pcm16 = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
        ref.write(pcm16.tobytes())
    except Exception as e:
        logger.debug("AEC reference write skipped: %s", e)


def reset() -> None:
    """Drop canceller state — call after an audio route change."""
    global _coupling_db
    # A route change moves the speaker, so the learned coupling no longer
    # describes this device pair.
    _coupling_db = None
    if _canceller is not None:
        _canceller.reset()
