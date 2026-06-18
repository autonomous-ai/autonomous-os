"""
Per-device preset overlay.

The base preset tables in ``hal.presets`` (``EMOTION_PRESETS``, ``SCENE_PRESETS``,
``AIM_PRESETS``) are the platform default — every device gets them. A device may
override only the values it wants different by shipping a sparse
``devices/<type>/presets.json``; this module deep-merges that delta onto the base
tables IN PLACE at startup, before any route or driver reads them. A device with
no ``presets.json`` keeps the base verbatim.

"Declare what's different" — the same philosophy as ``devices/_base`` inheritance,
applied to look/behaviour values (LED colors, scene mixes, servo aim positions).
This is HAL-only: the OS core (Go) does not read presets, so unlike capability
inheritance there is no cross-language parser to keep in sync.

Override file shape (every section optional)::

    {
      "led_count": 60,
      "emotion": { "listening": { "color": [255, 120, 0] } },
      "scene":   { "relax":     { "brightness": 0.3 } },
      "aim":     { "desk":      { "base_pitch.pos": 8.0 } }
    }

Each entry patches the matching base entry field-by-field: only the fields named
in the override change; everything else stays at the base value. An override that
names a preset absent from the base table is a typo (or a preset that does not
exist) → fail loud, mirroring DEVICE.md / SAFETY.md strictness. Field names within
an entry are intentionally permissive (the base entries themselves vary, e.g. some
emotions carry a "camera" key and some do not), so a device may add a field.
"""
import json
import logging
import os
from typing import Any, Dict

from hal.presets import AIM_PRESETS, EMOTION_PRESETS, SCENE_PRESETS, STATUS_LED_PRESETS

logger = logging.getLogger(__name__)

# LED ring size when a device declares none. The lamp reference ring is 32.
DEFAULT_LED_COUNT = 32

# Override section name -> the base table it patches. Mutated in place so every
# module that imported the table by reference sees the merged values.
_TABLES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "emotion": EMOTION_PRESETS,
    "scene": SCENE_PRESETS,
    "aim": AIM_PRESETS,
    "status_led": STATUS_LED_PRESETS,
}


def _merge_table(name: str, base: Dict[str, Dict], override: Any, device_type: str) -> None:
    """Deep-merge ``override`` onto ``base`` in place, one level deep. Each
    override entry patches an existing base entry field-by-field. An override key
    with no matching base entry fails loud — the common, silent-no-op typo."""
    if not isinstance(override, dict):
        raise ValueError(
            f"presets.json '{name}' for device '{device_type}' must be an object, "
            f"got {type(override).__name__}"
        )
    for key, fields in override.items():
        if key not in base:
            raise ValueError(
                f"presets.json '{name}.{key}' for device '{device_type}' overrides a "
                f"preset that does not exist; valid {name} keys: {sorted(base)}"
            )
        if not isinstance(fields, dict):
            raise ValueError(
                f"presets.json '{name}.{key}' for device '{device_type}' must be an "
                f"object of fields to override, got {type(fields).__name__}"
            )
        base[key].update(fields)


def apply_device_presets(device_type: str, devices_dir: str) -> int:
    """Overlay ``devices/<device_type>/presets.json`` onto the base preset tables
    in place and return the device's LED count (``DEFAULT_LED_COUNT`` if unset).

    Missing file → base tables unchanged, default LED count. A malformed file or an
    override of a non-existent preset → fail loud (a deploy fault, like DEVICE.md).
    """
    path = os.path.join(devices_dir, device_type, "presets.json")
    if not os.path.exists(path):
        logger.info("[presets] no per-device overrides for '%s' (using base presets)", device_type)
        return DEFAULT_LED_COUNT

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)  # JSONDecodeError → fail loud
    if not isinstance(data, dict):
        raise ValueError(f"presets.json for device '{device_type}' must be a JSON object")

    applied = []
    for name, table in _TABLES.items():
        if name in data:
            _merge_table(name, table, data[name], device_type)
            applied.append(name)

    led_count = data.get("led_count", DEFAULT_LED_COUNT)
    if not isinstance(led_count, int) or isinstance(led_count, bool) or led_count <= 0:
        raise ValueError(
            f"presets.json led_count for device '{device_type}' must be a positive "
            f"integer, got {led_count!r}"
        )

    logger.info(
        "[presets] applied per-device overrides for '%s': sections=%s led_count=%d",
        device_type, applied or ["none"], led_count,
    )
    return led_count
