"""The tracking master switch and the hot-swap-safe playback hooks."""

import re
from pathlib import Path

from hal.tracking import client, tts_hooks

HAL_ROOT = Path(__file__).resolve().parents[1]


def test_sending_is_off_by_default(monkeypatch, caplog):
    """Default OFF: nothing leaves the device — but the local log still has it,
    which is the whole point of keeping the log write ahead of the send."""
    monkeypatch.delenv(client.ENV_ENABLED, raising=False)
    sent = []
    monkeypatch.setattr(client, "_ensure_worker", lambda: sent.append("worker"))

    with caplog.at_level("INFO", logger="hal.tracking"):
        client.report("voice_metrics_interaction", {"outcome": "acknowledged"})

    assert sent == [], "no worker may start while sending is disabled"
    assert "[tracking] voice_metrics_interaction" in caplog.text
    assert "acknowledged" in caplog.text


def test_flag_accepts_the_usual_truthy_values(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(client.ENV_ENABLED, value)
        assert client.enabled() is True, value
    for value in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv(client.ENV_ENABLED, value)
        assert client.enabled() is False, value


def test_enabled_flag_lets_the_event_through(monkeypatch):
    monkeypatch.setenv(client.ENV_ENABLED, "1")
    started = []
    monkeypatch.setattr(client, "_ensure_worker", lambda: started.append(1))
    monkeypatch.setattr(client._queue, "put_nowait", lambda payload: None)

    client.report("voice_metrics_interaction", {"outcome": "acknowledged"})
    assert started == [1]


# --- hot swap ---------------------------------------------------------------

def test_every_tts_construction_site_wires_the_playback_hooks():
    """/voice/start hot-swaps TTSService when the provider or voice changes.
    A site that forgets these keeps speaking while the KPI goes blind, so every
    construction site must pass the shared hooks.

    Read as text rather than imported: importing hal.server initialises the
    production logging directory."""
    sites = 0
    for path in (HAL_ROOT / "server.py", HAL_ROOT / "routes" / "voice.py"):
        src = path.read_text()
        # `= TTSService(` only: VirtualTTSService is the simulator and plays
        # no real audio, so it has nothing to report.
        for match in re.finditer(r"=\s*TTSService\((.*?)\n\s*\)", src, re.S):
            body = match.group(1)
            sites += 1
            assert "on_playback_audio=tts_hooks.on_playback_audio" in body, path
            assert "on_playback_done=tts_hooks.on_playback_done" in body, path
    assert sites == 2, f"expected both construction sites, found {sites}"


def test_hooks_never_raise_into_the_audio_path(monkeypatch):
    import hal.tracking.voice_metrics as voice_metrics

    def boom(*_a, **_k):
        raise RuntimeError("tracker exploded")

    monkeypatch.setattr(voice_metrics, "playback_audio", boom)
    monkeypatch.setattr(voice_metrics, "playback_end", boom)

    tts_hooks.on_playback_audio("run:x", "cached")   # must not raise
    tts_hooks.on_playback_done()
