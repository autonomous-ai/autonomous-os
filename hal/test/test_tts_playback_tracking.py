"""The playback-tracking hooks on TTSService.

The point of these: `on_speak_start` is NOT proof of playback — the cached
path fires it before it has taken the stream lock or written a byte. Tracking
must key off the first frame that actually reaches the stream, carrying the
owner that claimed the speaker.
"""

import threading
import wave
from pathlib import Path

import numpy as np
import pytest

from hal.drivers.voice.tts.service import TTSService


class _FakeStream:
    def __init__(self, recorder):
        self._recorder = recorder

    def write(self, data):
        self._recorder.append(len(data))


def _playback_service(tmp_path, writes, fired, stopped=None):
    """TTSService shell with just enough state for _play_wav_inline."""
    service = object.__new__(TTSService)
    service._np = np
    service._sd = object()
    service._backend = None
    service._stream_lock = threading.Lock()
    service._stop_event = threading.Event()
    service._device_rate = 24000
    service._stream_rate = 24000
    service._on_speak_start = lambda: fired.append("speak_start")
    service._on_playback_audio = lambda owner, kind: fired.append(("audio", owner, kind))
    service._on_playback_done = lambda: (stopped or []).append("done")
    service._audio_written_fired = False
    service._playback_owner = ""
    service._ensure_stream = lambda rate: _FakeStream(writes)
    service._resample = lambda samples, src, dst: samples
    return service


def _wav(tmp_path: Path, seconds=0.2, rate=24000) -> Path:
    path = tmp_path / "phrase.wav"
    samples = (np.zeros(int(rate * seconds)) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return path


def test_audio_hook_fires_only_after_a_real_write(tmp_path):
    writes, fired = [], []
    service = _playback_service(tmp_path, writes, fired)
    service._begin_playback("run:run-7")

    service._play_wav_inline(_wav(tmp_path))

    assert writes, "test setup: expected frames to be written"
    kinds = [f for f in fired if isinstance(f, tuple)]
    assert kinds == [("audio", "run:run-7", "cached")]
    # on_speak_start fired first and is NOT the ack signal — that ordering is
    # exactly why the tracking hook exists.
    assert fired[0] == "speak_start"


def test_no_audio_hook_when_playback_is_stopped_before_writing(tmp_path):
    """Stopped between the callback and the first write: nothing was heard, so
    nothing may be reported as heard."""
    writes, fired = [], []
    service = _playback_service(tmp_path, writes, fired)
    service._begin_playback("run:run-7")
    service._stop_event.set()

    service._play_wav_inline(_wav(tmp_path))

    assert writes == []
    assert [f for f in fired if isinstance(f, tuple)] == []
    assert fired == ["speak_start"]


def test_audio_hook_fires_once_per_playback(tmp_path):
    writes, fired = [], []
    service = _playback_service(tmp_path, writes, fired)
    service._begin_playback("run:run-7")

    service._play_wav_inline(_wav(tmp_path, seconds=0.5))

    assert len(writes) > 1, "test setup: expected several blocks"
    assert len([f for f in fired if isinstance(f, tuple)]) == 1


def test_unclaimed_playback_reports_an_empty_owner(tmp_path):
    writes, fired = [], []
    service = _playback_service(tmp_path, writes, fired)
    service._begin_playback("")

    service._play_wav_inline(_wav(tmp_path))

    assert [f for f in fired if isinstance(f, tuple)] == [("audio", "", "cached")]


@pytest.mark.parametrize("owner", ["run:abc", "interaction:vi-1", ""])
def test_begin_playback_rearms_the_hook_for_each_playback(tmp_path, owner):
    writes, fired = [], []
    service = _playback_service(tmp_path, writes, fired)
    wav = _wav(tmp_path)

    service._begin_playback("run:first")
    service._play_wav_inline(wav)
    service._begin_playback(owner)
    service._play_wav_inline(wav)

    owners = [f[1] for f in fired if isinstance(f, tuple)]
    assert owners == ["run:first", owner]
