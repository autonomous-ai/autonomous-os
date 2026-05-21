"""Action analysis: model lifecycle, person detection, and session management.

Wraps a HumanActionRecognizerModel + optional PersonDetector.
Each WebSocket connection creates an ActionSession via create_session().
"""

import asyncio

from typing_extensions import override

from core.models.action import ActionPerceptionSessionConfig
from core.perception.action.predictors.base import HumanActionRecognizer
from core.perception.action.session import ActionPerceptionSession
from core.perception.action.utils import ActionRecognizerFactory
from core.perception.base import PerceptionBase
from core.perception.person.predictors import PersonDetector
from core.perception.person.utils import PersonDetectorFactory


class ActionPerception(PerceptionBase[ActionPerceptionSession]):
    """Action recognition pipeline. Loaded once, shared by all WS sessions."""

    def __init__(
        self,
        action_recognizer_factory: ActionRecognizerFactory,
        person_detector_factory: PersonDetectorFactory | None = None,
        default_config: ActionPerceptionSessionConfig | None = None,
    ):
        super().__init__()

        self._action_recognizer_factory: ActionRecognizerFactory = action_recognizer_factory
        self._person_detector_factory: PersonDetectorFactory | None = person_detector_factory
        self._default_config: ActionPerceptionSessionConfig | None = default_config

        self._action_recognizer: HumanActionRecognizer | None = None
        self._person_detector: PersonDetector | None = None
        self._running: bool = False

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._action_recognizer = self._action_recognizer_factory.create()
        await asyncio.to_thread(self._action_recognizer.start)

        if self._person_detector_factory is not None:
            self._person_detector = self._person_detector_factory.create()
            await asyncio.to_thread(self._person_detector.start)

        self._running = True
        self._logger.info("Ready")

    @override
    async def stop(self) -> None:
        if self._action_recognizer is not None:
            await asyncio.to_thread(self._action_recognizer.stop)
            self._action_recognizer = None

        if self._person_detector is not None:
            await asyncio.to_thread(self._person_detector.stop)
            self._person_detector = None

        self._running = False
        self._logger.info("Stopped")

    @override
    def is_ready(self) -> bool:
        if not self._running or self._action_recognizer is None:
            return False
        if not self._action_recognizer.is_ready():
            return False
        return True

    @override
    async def create_session(self) -> ActionPerceptionSession:
        if self._action_recognizer is None:
            raise RuntimeError("ActionPerception not started")

        config = self._default_config or ActionPerceptionSession.DEFAULT_CONFIG
        return ActionPerceptionSession(
            action_recognizer=self._action_recognizer,
            person_detector=self._person_detector,
            config=config,
        )
