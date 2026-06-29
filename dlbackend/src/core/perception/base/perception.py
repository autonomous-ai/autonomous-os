import logging
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

SESSION_T = TypeVar("SESSION_T")


class PerceptionBase(Generic[SESSION_T], ABC):
    def __init__(self) -> None:
        self._logger: logging.Logger = logging.getLogger(
            f"{self.__class__.__module__}.{self.__class__.__name__}"
        )

        self._sessions_dict: dict[str, SESSION_T] = {}

    @abstractmethod
    async def start(self) -> None:
        pass

    @abstractmethod
    async def stop(self) -> None:
        pass

    @abstractmethod
    def is_ready(self) -> bool:
        pass

    @abstractmethod
    async def create_session(self) -> SESSION_T:
        pass
