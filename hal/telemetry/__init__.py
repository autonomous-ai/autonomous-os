"""Device telemetry for HAL.

`client` is the generic pipe (bounded queue → os-server → warehouse, with a
local log line for every event). `voice_metrics` is the first tracker built on it.
"""

from hal.telemetry import client, voice_metrics  # noqa: F401
