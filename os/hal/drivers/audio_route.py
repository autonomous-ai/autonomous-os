"""Runtime audio routing — hot-swap TTS output + VoiceService input between
the device's built-in speaker/mic and a connected Bluetooth headset.

Routing strategy (matches the Pi/OrangePi PulseAudio setup):

  * Built-in mode (default): TTS writes directly to the ALSA plughw device,
    VoiceService captures from its plughw mic. Same path hal had before.
  * Bluetooth mode: `pactl set-default-sink <bluez_sink>`, then point TTS
    at the PortAudio `pulse` device so every byte routes through PulseAudio
    onto the BT sink. STT input keeps the built-in mic unless the headset
    exposes a true HFP source.

This module never edits TTS/VoiceService source files; it mutates instance
attributes of already-initialized services from the outside. Sensing stays
on its own captured device pointer and is untouched.
"""

import logging
import threading
from typing import Optional

import hal.app_state as state

logger = logging.getLogger("hal.audio_route")

_lock = threading.Lock()

_BUILTIN_OUT_IDX: Optional[int] = None
_BUILTIN_IN_IDX: Optional[int] = None
_BUILTIN_ALSA_IN: Optional[str] = None
_BUILTIN_PA_SINK: Optional[str] = None
_BUILTIN_PA_SOURCE: Optional[str] = None
_defaults_captured: bool = False

_current_label: str = "builtin"


def _capture_builtin_defaults() -> None:
    """Latch the built-in device indices + the PulseAudio default sink/source
    on first call. Re-running is a no-op so we never overwrite the originals."""
    global _BUILTIN_OUT_IDX, _BUILTIN_IN_IDX, _BUILTIN_ALSA_IN
    global _BUILTIN_PA_SINK, _BUILTIN_PA_SOURCE, _defaults_captured
    if _defaults_captured:
        return
    _BUILTIN_OUT_IDX = state.audio_output_device
    _BUILTIN_IN_IDX = state.audio_input_device
    try:
        from hal.config import AUDIO_INPUT_ALSA
        _BUILTIN_ALSA_IN = AUDIO_INPUT_ALSA
    except Exception:
        _BUILTIN_ALSA_IN = None
    try:
        from hal.drivers.bluetooth_manager import BluetoothManager
        mgr = BluetoothManager()
        _BUILTIN_PA_SINK = mgr.pa_default_sink()
        _BUILTIN_PA_SOURCE = mgr.pa_default_source()
    except Exception:
        _BUILTIN_PA_SINK = None
        _BUILTIN_PA_SOURCE = None
    _defaults_captured = True
    logger.info(
        "Built-in audio defaults captured: out_idx=%s in_idx=%s alsa_in=%s pa_sink=%s pa_source=%s",
        _BUILTIN_OUT_IDX, _BUILTIN_IN_IDX, _BUILTIN_ALSA_IN, _BUILTIN_PA_SINK, _BUILTIN_PA_SOURCE,
    )


def current_label() -> str:
    return _current_label


def _swap_tts(output_idx: Optional[int]) -> None:
    tts = state.tts_service
    if tts is None:
        return
    try:
        if tts.speaking:
            tts.stop()
    except Exception:
        logger.exception("tts.stop failed")
    try:
        tts.release_stream()
    except Exception:
        logger.exception("tts.release_stream failed")
    try:
        tts._output_device = output_idx
        tts._device_rate = None
        tts._stream = None
        tts._stream_rate = None
        if tts._sd is not None:
            tts._probe_device_rate(force=True)
            if tts._device_rate:
                tts._ensure_stream(tts._device_rate)
    except Exception:
        logger.exception("tts device swap failed")


def _swap_voice(input_idx: Optional[int], alsa_device: Optional[str]) -> None:
    vs = state.voice_service
    if vs is None:
        return
    try:
        vs.stop()
    except Exception:
        logger.exception("voice_service.stop failed")
    try:
        vs._input_device = input_idx
        vs._alsa_device = alsa_device
        vs._device_rate = None
        vs.start()
    except Exception:
        logger.exception("voice_service restart failed")


def route_to_builtin() -> None:
    """Switch TTS + voice back to the device's built-in speaker/mic and restore
    the PulseAudio default sink to whatever it was before we touched it."""
    global _current_label
    _capture_builtin_defaults()
    with _lock:
        logger.info(
            "Route → built-in (out=%s in=%s pa_sink=%s)",
            _BUILTIN_OUT_IDX, _BUILTIN_IN_IDX, _BUILTIN_PA_SINK,
        )
        if _BUILTIN_PA_SINK:
            try:
                from hal.drivers.bluetooth_manager import BluetoothManager
                BluetoothManager().set_pa_default_sink(_BUILTIN_PA_SINK)
            except Exception:
                logger.exception("PA default-sink restore failed")
        _swap_tts(_BUILTIN_OUT_IDX)
        _swap_voice(_BUILTIN_IN_IDX, _BUILTIN_ALSA_IN)
        _current_label = "builtin"


def route_to_bluetooth_pa(
    pulse_sd_index: int,
    pa_sink_name: str,
    pa_source_name: Optional[str],
    mac: str,
) -> None:
    """Switch to BT via PulseAudio. Sets the PA default sink (and source, if
    the headset exposes a real HFP source) and points TTS at PortAudio's
    generic `pulse` device. STT input falls back to the built-in mic when the
    headset is A2DP-only — most cheap BT speakers/AirPods in A2DP profile."""
    global _current_label
    _capture_builtin_defaults()
    with _lock:
        logger.info(
            "Route → bt:%s (pulse_sd=%s sink=%s source=%s)",
            mac, pulse_sd_index, pa_sink_name, pa_source_name,
        )
        try:
            from hal.drivers.bluetooth_manager import BluetoothManager
            mgr = BluetoothManager()
            mgr.set_pa_default_sink(pa_sink_name)
            if pa_source_name:
                mgr.set_pa_default_source(pa_source_name)
        except Exception:
            logger.exception("PA default-sink swap failed")

        _swap_tts(pulse_sd_index)

        if pa_source_name:
            # HFP source available — point voice at `pulse` so STT reads from
            # the BT mic. Drop the ALSA plughw override so VoiceService uses
            # the sd.InputStream(device=pulse_sd_index) path.
            _swap_voice(pulse_sd_index, None)
        else:
            # A2DP-only headset → keep the built-in mic for STT so the user can
            # still talk to the device while listening through the headset.
            _swap_voice(_BUILTIN_IN_IDX, _BUILTIN_ALSA_IN)
        _current_label = f"bt:{mac}"


def maybe_restore_bt_route() -> None:
    """Called once at server startup. If the user had a BT headset active
    before reboot, try to reconnect + re-route. Best effort — failures fall
    back silently to the built-in route already in place."""
    try:
        from hal.drivers.bluetooth_manager import BluetoothManager
    except Exception:
        return
    mgr = BluetoothManager()
    mac = mgr.active_mac
    if not mac or not mgr.available():
        return
    logger.info("Restoring BT route to %s on boot", mac)
    try:
        import sounddevice as sd
    except Exception:
        logger.info("BT restore skipped — sounddevice unavailable")
        return
    try:
        if not mgr.info(mac)["connected"]:
            mgr.connect(mac)
        sink = mgr.pa_sink_for_mac(mac)
        if not sink:
            logger.warning("BT restore: PulseAudio has no sink for %s", mac)
            return
        pulse_idx = mgr.pulse_sd_index(sd)
        if pulse_idx is None:
            logger.warning("BT restore: PortAudio has no `pulse` device")
            return
        source = mgr.pa_source_for_mac(mac)
        route_to_bluetooth_pa(pulse_idx, sink, source, mac)
    except Exception:
        logger.exception("BT route restore failed")
