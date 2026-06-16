"""Bluetooth headset routes — pair / connect / route TTS+STT to a BT
headset so the user can use the device privately without disturbing others.

Endpoints live under /bluetooth/* (exposed to the web UI via /hw/bluetooth/*
through the existing OS server reverse proxy).
"""

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import hal.app_state as state
from hal.drivers.audio_route import (
    current_label,
    route_to_bluetooth_pa,
    route_to_builtin,
)
from hal.drivers.bluetooth_manager import BluetoothManager

logger = logging.getLogger("hal.bluetooth.routes")

router = APIRouter(prefix="/bluetooth", tags=["Bluetooth"])

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore

_manager: Optional[BluetoothManager] = None


def _mgr() -> BluetoothManager:
    global _manager
    if _manager is None:
        _manager = BluetoothManager()
    return _manager


class MacRequest(BaseModel):
    mac: str


class ActiveRequest(BaseModel):
    mac: Optional[str] = None  # None / "" → switch back to the device


@router.get("/available")
def bt_available():
    """Tell the UI whether bluetoothctl works on this host."""
    return {"available": _mgr().available()}


@router.post("/scan/start")
def bt_scan_start():
    mgr = _mgr()
    if not mgr.available():
        raise HTTPException(503, "bluetoothctl not available")
    mgr.scan_start()
    return {"status": "ok"}


@router.get("/scan/results")
def bt_scan_results():
    mgr = _mgr()
    paired_macs = {d["mac"] for d in mgr.paired_devices()}
    discovered = [d for d in mgr.discovered_devices() if d["mac"] not in paired_macs]
    return {"scanning": mgr.scan_active(), "devices": discovered}


@router.get("/devices")
def bt_devices():
    mgr = _mgr()
    paired = mgr.paired_devices()
    active = mgr.active_mac
    for d in paired:
        d["active"] = d["mac"] == active
    return {"active_mac": active, "label": current_label(), "devices": paired}


@router.post("/pair")
def bt_pair(req: MacRequest):
    mgr = _mgr()
    if not mgr.available():
        raise HTTPException(503, "bluetoothctl not available")
    if not mgr.pair(req.mac):
        raise HTTPException(
            500,
            "Pairing failed — make sure the headset is in pairing mode and try again",
        )
    return {"status": "ok"}


@router.post("/forget")
def bt_forget(req: MacRequest):
    mgr = _mgr()
    target = req.mac.upper()
    if mgr.active_mac and mgr.active_mac == target:
        # Bring TTS/STT back to the device before it disappears,
        # otherwise the persistent OutputStream is left pointed at a gone sink.
        route_to_builtin()
    if not mgr.forget(target):
        raise HTTPException(500, "Forget failed")
    return {"status": "ok"}


@router.get("/active")
def bt_active_get():
    mgr = _mgr()
    return {"active_mac": mgr.active_mac, "label": current_label()}


@router.post("/active")
def bt_active_set(req: ActiveRequest):
    """Toggle voice routing.

    mac = null / "" → route back to the device's built-in speaker + mic.
    mac = MAC       → ensure connected, find PortAudio indices, route TTS+STT
                      to the BT device. STT mic falls back to the device mic if the
                      device has no input (BT speaker case).
    """
    mgr = _mgr()
    target = (req.mac or "").strip().upper() or None

    if target is None:
        route_to_builtin()
        mgr.set_active_mac(None)
        return {"status": "ok", "active_mac": None, "label": current_label()}

    if sd is None:
        raise HTTPException(503, "sounddevice not available on host")

    if not mgr.info(target)["connected"] and not mgr.connect(target):
        raise HTTPException(503, f"Could not connect to {target}")

    # Auto-switch to HFP whenever available — single-device mic+speaker is
    # the whole point of "use headset" mode. Falls back to whatever profile
    # is active if HFP isn't offered (e.g. BT speakers).
    card = mgr.pa_card_for_mac(target)
    if card:
        profiles = mgr.pa_card_profiles(card)
        if profiles.get("handsfree_head_unit", False):
            mgr.set_pa_card_profile(card, "handsfree_head_unit")

    # Poll for the sink to appear instead of a fixed sleep — PA exposes the
    # bluez sink asynchronously after a profile switch / reconnect, and on
    # a flaky BT chip it can take a few seconds. Up to ~8s.
    pa_sink: Optional[str] = None
    for _ in range(8):
        pa_sink = mgr.pa_sink_for_mac(target)
        if pa_sink:
            break
        time.sleep(1.0)
    if not pa_sink:
        raise HTTPException(
            503,
            f"PulseAudio has no sink for {target} — check pulseaudio-module-bluetooth",
        )
    pulse_idx = mgr.pulse_sd_index(sd)
    if pulse_idx is None:
        raise HTTPException(
            503,
            "PortAudio cannot see PulseAudio — sounddevice/portaudio not built with pulse support",
        )
    pa_source = mgr.pa_source_for_mac(target)

    route_to_bluetooth_pa(pulse_idx, pa_sink, pa_source, target)
    mgr.set_active_mac(target)
    return {
        "status": "ok",
        "active_mac": target,
        "label": current_label(),
        "pa_sink": pa_sink,
        "pa_source": pa_source,
        "pulse_sd_index": pulse_idx,
    }


@router.get("/status")
def bt_status():
    """Snapshot for the monitor card — what's active, what's around."""
    mgr = _mgr()
    return {
        "available": mgr.available(),
        "active_mac": mgr.active_mac,
        "label": current_label(),
        "scanning": mgr.scan_active(),
        "paired": mgr.paired_devices(),
    }
