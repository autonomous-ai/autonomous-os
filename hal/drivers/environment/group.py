"""Compose independent sensor workers into one environmental snapshot."""

import math
from functools import partial

from hal.board.environment import load_environment_components
from hal.board.sen55 import load_sen55_config, SEN55Timing
from hal.board.scd41 import load_scd41_config, SCD41Timing
from hal.drivers.environment.sen55 import SEN55, FIELDS as SEN55_FIELDS
from hal.drivers.environment.scd41 import SCD41
from hal.drivers.environment.service import EnvironmentService


# A component owns each public measurement; adding a sensor cannot silently
# overwrite another sensor's temperature/humidity or change their provenance.
COMPONENT_FIELDS = {"sen55": SEN55_FIELDS, "scd41": ("co2_ppm",)}


def create_environment_group(device_dir, board_id, simulation=False):
    registry = {
        "sen55": (load_sen55_config, SEN55, SEN55Timing),
        "scd41": (load_scd41_config, SCD41, SCD41Timing),
    }
    workers = {}
    for name in load_environment_components(device_dir):
        loader, driver, timing = registry[name]
        cfg = None if simulation else loader(device_dir, board_id)
        factory = driver
        if name == "scd41" and cfg is not None:
            factory = partial(driver, automatic_self_calibration=cfg.automatic_self_calibration)
        workers[name] = EnvironmentService(
            enabled=cfg is not None,
            bus=cfg.bus if cfg else None,
            timing=cfg.timing if cfg else timing(),
            driver_factory=factory,
            name=name,
        )
    return EnvironmentGroup(workers)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class EnvironmentGroup:
    def __init__(self, components):
        self.components = dict(components)

    def start(self):
        for worker in self.components.values():
            worker.start()

    def stop(self):
        for worker in self.components.values():
            worker.stop()

    def snapshot(self):
        components = {name: worker.snapshot() for name, worker in self.components.items()}
        sample, sources, timestamps = {}, {}, {}
        ages, errors, active = [], [], []
        unavailable = False
        for name, component in components.items():
            fields = COMPONENT_FIELDS[name]
            for field in fields:
                sample[field] = None
                sources[field] = name
            if not component["enabled"]:
                continue
            active.append(component)
            values = component.get("sample") or {}
            stamp, age = values.get("timestamp"), component.get("age_s")
            status = values.get("device_status")
            valid = (
                component["state"] == "ready" and not component["stale"]
                and _finite(stamp) and stamp > 0
                and _finite(age) and age >= 0
                and (status is None or status == 0)
            )
            if component.get("last_error"):
                errors.append(f"{name}: {component['last_error']}")
            elif status is not None and status != 0:
                errors.append(f"{name}: sensor status reports a fault ({status})")
            added = False
            if valid:
                for field in fields:
                    value = values.get(field)
                    if _finite(value):
                        sample[field] = value
                        timestamps[field] = stamp
                        added = True
                if added:
                    ages.append(age)
            if not added:
                unavailable = True

        ready = bool(timestamps)
        if ready:
            sample["timestamp"] = max(timestamps.values())
            state = "ready"
        elif not active:
            state = "disabled"
        elif errors or any(c["state"] == "error" for c in active):
            state = "error"
        elif all(c["state"] == "stopped" for c in active):
            state = "stopped"
        else:
            state = "starting"
        result = {
            "state": state, "enabled": bool(active), "stale": not ready,
            "partial": ready and unavailable,
            "last_error": "; ".join(errors) or None,
            "sample": sample if ready else None,
            "age_s": min(ages) if ages else None,
            "components": components,
            "sources": sources,
            "metric_timestamps": timestamps,
        }
        # Keep single-component acquisition diagnostics convenient for legacy
        # clients. Multi-component timings/buses live under components only.
        if len(components) == 1:
            only = next(iter(components.values()))
            result.update(bus=only["bus"], timing=only["timing"])
            if ready and "sen55" in components and "device_status" in only["sample"]:
                sample["device_status"] = only["sample"]["device_status"]
        return result
