"""Output data models streamed from the realtime voice agent."""

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict
from typing_extensions import ClassVar

from hal.drivers.realtime.enums import OutputTypeEnum


class OutputBase(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, arbitrary_types_allowed=True
    )
    type: OutputTypeEnum


class TextOutput(OutputBase):
    type: OutputTypeEnum = OutputTypeEnum.TEXT
    text: str


class AudioOutput(OutputBase):
    type: OutputTypeEnum = OutputTypeEnum.AUDIO
    audio: npt.NDArray[np.float32]
    transcript: str | None = None


class FunctionCallOutput(OutputBase):
    type: OutputTypeEnum = OutputTypeEnum.FUNCTION_CALL
    name: str
    arguments: str  # JSON string
    call_id: str
