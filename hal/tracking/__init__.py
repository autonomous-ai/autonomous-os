"""Product-analytics tracking for HAL.

`client` is the generic pipe (bounded queue → os-server → warehouse, with a
local log line for every event). `voice_kpi` is the first tracker built on it.
"""

from hal.tracking import client, voice_kpi  # noqa: F401
