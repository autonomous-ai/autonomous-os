"""Shared model state for protocol handlers.

Lifespan (server.py) calls setters during startup/shutdown.
Routers call getters to access the loaded models.
"""

from core.perception.action.perception import ActionPerception
from core.perception.emotion.perception import EmotionPerception
from core.perception.object.perception import ObjectPerception
from core.perception.pose.perception import PosePerception

_action_model: ActionPerception | None = None
_emotion_model: EmotionPerception | None = None
_pose_model: PosePerception | None = None
_object_models: dict[str, ObjectPerception] = {}


def get_action_model() -> ActionPerception | None:
    return _action_model


def set_action_model(model: ActionPerception | None) -> None:
    global _action_model
    _action_model = model


def get_emotion_model() -> EmotionPerception | None:
    return _emotion_model


def set_emotion_model(model: EmotionPerception | None) -> None:
    global _emotion_model
    _emotion_model = model


def get_pose_model() -> PosePerception | None:
    return _pose_model


def set_pose_model(model: PosePerception | None) -> None:
    global _pose_model
    _pose_model = model


def get_object_models() -> dict[str, ObjectPerception]:
    return _object_models


def get_object_model(name: str) -> ObjectPerception | None:
    return _object_models.get(name)


def set_object_models(models: dict[str, ObjectPerception]) -> None:
    global _object_models
    _object_models = models
