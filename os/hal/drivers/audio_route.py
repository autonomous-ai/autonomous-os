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
from contextlib import contextmanager
from typing import Optional

import hal.app_state as state

logger = logging.getLogger("hal.audio_route")

_lock = threading.Lock()

# Serializes ENTIRE route operations (quiesce → PortAudio re-init → swap).
# Boot-restore, "use headset" and "disconnect" can otherwise interleave:
# one flow re-initializes PortAudio while another's streams are live →
# SIGABRT in the C layer (observed killing hal.service twice). Every caller
# that touches pulse_sd_index()/route_to_* must hold this for its whole flow.
route_op_lock = threading.RLock()

# PortAudio re-init vs stream-open exclusion. route_op_lock serializes route
# flows against EACH OTHER, but not against threads that open sounddevice
# streams on their own clock — above all TTS, whose speak() fires from agent
# turns at any moment. quiesce_portaudio_users() closes the streams that exist
# but nothing STOPPED a new one from opening in the window before
# Pa_Terminate() — that exact race SIGABRT'd hal.service twice on 2026-07-06
# (BT route flow re-init vs a concurrent stream open). Protocol:
#   * pa_reinit_event is set for the whole quiesce→terminate→init span;
#   * pa_reinit_lock is held around Pa_Terminate()+Pa_Initialize() itself;
#   * every sounddevice stream OPEN goes through stream_open_guard(), which
#     takes the lock and refuses (RuntimeError) while the event is set — one
#     failed TTS write beats a dead process, and the write-watchdog/fallback
#     already handles TTS write failures.
# Lock-order note: stream openers hold their own locks (e.g. TTS _stream_lock)
# BEFORE taking pa_reinit_lock; the re-init path must therefore never grab an
# opener's lock while holding pa_reinit_lock (it quiesces BEFORE locking).
pa_reinit_lock = threading.RLock()
pa_reinit_event = threading.Event()


@contextmanager
def stream_open_guard():
    """Guard a sounddevice stream open against a concurrent PortAudio re-init.

    Raises RuntimeError instead of opening while a re-init window is active:
    a stream that comes alive between quiesce and Pa_Terminate() aborts the
    whole process, and the caller can always retry the open on its next write.
    """
    with pa_reinit_lock:
        if pa_reinit_event.is_set():
            raise RuntimeError(
                "PortAudio re-init in progress — stream open rejected"
            )
        yield

_BUILTIN_OUT_IDX: Optional[int] = None
_BUILTIN_IN_IDX: Optional[int] = None
_BUILTIN_OUT_NAME: Optional[str] = None
_BUILTIN_IN_NAME: Optional[str] = None
_BUILTIN_ALSA_IN: Optional[str] = None
_BUILTIN_PA_SINK: Optional[str] = None
_BUILTIN_PA_SOURCE: Optional[str] = None
_defaults_captured: bool = False

_current_label: str = "builtin"


def _capture_builtin_defaults() -> None:
    """Latch the built-in device indices + the PulseAudio default sink/source
    on first call. Re-running is a no-op so we never overwrite the originals."""
    global _BUILTIN_OUT_IDX, _BUILTIN_IN_IDX, _BUILTIN_ALSA_IN
    global _BUILTIN_OUT_NAME, _BUILTIN_IN_NAME
    global _BUILTIN_PA_SINK, _BUILTIN_PA_SOURCE, _defaults_captured
    if _defaults_captured:
        return
    _BUILTIN_OUT_IDX = state.audio_output_device
    _BUILTIN_IN_IDX = state.audio_input_device
    # Also remember device NAMES: pulse_sd_index() re-initializes PortAudio,
    # which can renumber devices — a captured int would then point at the
    # wrong device. Restore resolves by name first, index as fallback.
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        if _BUILTIN_OUT_IDX is not None and _BUILTIN_OUT_IDX < len(devs):
            _BUILTIN_OUT_NAME = devs[_BUILTIN_OUT_IDX]["name"]
        if _BUILTIN_IN_IDX is not None and _BUILTIN_IN_IDX < len(devs):
            _BUILTIN_IN_NAME = devs[_BUILTIN_IN_IDX]["name"]
    except Exception:
        _BUILTIN_OUT_NAME = _BUILTIN_IN_NAME = None
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
        # PulseAudio outlives hal restarts, so at capture time its default
        # sink/source may still be a bluez device from the previous session.
        # Latching that would make route_to_builtin "restore" audio to the
        # headset — poisoned snapshot. A BT device is never the built-in.
        if _BUILTIN_PA_SINK and _BUILTIN_PA_SINK.startswith("bluez"):
            _BUILTIN_PA_SINK = None
        if _BUILTIN_PA_SOURCE and _BUILTIN_PA_SOURCE.startswith("bluez"):
            _BUILTIN_PA_SOURCE = None
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


def bt_active() -> bool:
    """True while audio output routes to a Bluetooth headset (label 'bt:<MAC>').
    Players that bypass PortAudio (e.g. music via aplay) check this to pick a
    PulseAudio output instead, so they land on the same headset as TTS."""
    return _current_label.startswith("bt:")


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


def quiesce_portaudio_users() -> None:
    """Close every live sounddevice stream before a PortAudio re-init.

    pulse_sd_index() tears PortAudio down and re-initializes it so a freshly
    started PulseAudio server becomes visible. Doing that while the TTS
    persistent stream or the mic capture stream is still open aborts the
    whole process (SIGABRT in the C layer — observed killing hal.service).
    The route swap that follows reopens everything; on failure paths call
    resume_capture() so the mic doesn't stay dead."""
    tts = state.tts_service
    if tts is not None:
        try:
            if getattr(tts, "speaking", False):
                tts.stop()
            if hasattr(tts, "release_stream"):
                tts.release_stream()
        except Exception:
            logger.exception("TTS quiesce failed")
    vs = state.voice_service
    if vs is not None:
        try:
            vs.stop()
        except Exception:
            logger.exception("voice quiesce failed")


def resume_capture() -> None:
    """Restart mic capture after quiesce_portaudio_users() when the route
    flow bailed out before its swap could do it."""
    vs = state.voice_service
    if vs is not None:
        try:
            vs.start()
        except Exception:
            logger.exception("voice resume failed")


def _resolve_by_name(name: Optional[str], fallback_idx: Optional[int], output: bool) -> Optional[int]:
    """Find the current PortAudio index for a captured device name. Indices
    can shift after pulse_sd_index()'s PortAudio re-init, so the name is the
    stable key; the captured index is only a last-resort fallback."""
    if name:
        try:
            import sounddevice as sd
            key = "max_output_channels" if output else "max_input_channels"
            for i, d in enumerate(sd.query_devices()):
                if d["name"] == name and d[key] > 0:
                    if i != fallback_idx:
                        logger.info(
                            "Device '%s' moved: idx %s → %s after PortAudio re-init",
                            name, fallback_idx, i,
                        )
                    return i
        except Exception:
            logger.exception("device resolve by name failed")
    return fallback_idx


def route_to_builtin() -> None:
    """Switch TTS + voice back to the device's built-in speaker/mic and restore
    the PulseAudio default sink to whatever it was before we touched it."""
    global _current_label
    _capture_builtin_defaults()
    with _lock:
        out_idx = _resolve_by_name(_BUILTIN_OUT_NAME, _BUILTIN_OUT_IDX, output=True)
        in_idx = _resolve_by_name(_BUILTIN_IN_NAME, _BUILTIN_IN_IDX, output=False)
        logger.info(
            "Route → built-in (out=%s in=%s pa_sink=%s)",
            out_idx, in_idx, _BUILTIN_PA_SINK,
        )
        try:
            from hal.drivers.bluetooth_manager import BluetoothManager
            mgr = BluetoothManager()
            sink = _BUILTIN_PA_SINK or mgr.first_alsa_sink()
            if sink:
                mgr.set_pa_default_sink(sink)
        except Exception:
            logger.exception("PA default-sink restore failed")
        _swap_tts(out_idx)
        _swap_voice(in_idx, _BUILTIN_ALSA_IN)
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
        # Hold the route-op lock for the whole quiesce→re-init→swap sequence
        # so an early "use headset" click can't interleave with boot restore.
        with route_op_lock:
            quiesce_portaudio_users()
            pulse_idx = mgr.pulse_sd_index(sd)
            if pulse_idx is None:
                logger.warning("BT restore: PortAudio has no `pulse` device")
                resume_capture()
                return
            source = mgr.pa_source_for_mac(mac)
            route_to_bluetooth_pa(pulse_idx, sink, source, mac)
    except Exception:
        logger.exception("BT route restore failed")
        resume_capture()
