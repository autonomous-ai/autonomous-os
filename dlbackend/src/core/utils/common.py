from copy import deepcopy
from typing import TypeVar

T = TypeVar("T")


def get_or_default(x: T | None, default: T) -> T:
    if x is None:
        return deepcopy(default)
    return deepcopy(x)
