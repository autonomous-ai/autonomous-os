import logging
import time
from typing import Any, cast, override

import cv2
import numpy as np
import numpy.typing as npt

import lelamp.config as config
from lelamp.service.sensing.perceptions.typing import SendEventCallable
from lelamp.service.sensing.perceptions.utils import PerceptionStateObservers

from .base import Perception

logger = logging.getLogger(__name__)


class LightLevelPerception(Perception[cv2.typing.MatLike]):
    """Detects significant ambient light changes via mean frame brightness."""

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
    ):
        super().__init__(perception_state, send_event)
        self._last_level: float | None = None
        self._last_check: float = 0.0

    @override
    def cleanup(self) -> None:
        pass

    @override
    def _check_impl(self, data: cv2.typing.MatLike):
        frame = data

        if frame is None:
            logger.debug("[light] frame is None, skipping")
            return

        now = time.time()
        if now - self._last_check < config.LIGHT_LEVEL_INTERVAL_S:
            return
        self._last_check = now

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness = float(np.mean(cast(npt.NDArray[np.uint8], gray)))

        prev = self._last_level
        self._last_level = brightness

        if prev is not None:
            change = brightness - prev
            if abs(change) >= config.LIGHT_CHANGE_THRESHOLD:
                if change < 0:
                    msg = f"Ambient light decreased significantly (level: {brightness:.0f}/255, change: {change:.0f})"
                else:
                    msg = f"Ambient light increased significantly (level: {brightness:.0f}/255, change: {change:+.0f})"
                self._send_event(
                    "light.level",
                    msg,
                    "light",
                    None,
                    config.LIGHT_LEVEL_INTERVAL_S,
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "light_level",
            "level": round(self._last_level, 1)
            if self._last_level is not None
            else None,
            "seconds_since_check": int(time.time() - self._last_check)
            if self._last_check
            else None,
        }
