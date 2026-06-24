from copy import deepcopy
from typing import Any, TypeVar

T = TypeVar("T")


def get_or_default(x: T | None, default: T) -> T:
    if x is None:
        return deepcopy(default)
    return deepcopy(x)


def deep_hash(v: Any) -> int:
    """Recursively convert a value to a stable hash.

    Handles nested dicts, lists, sets, tuples, numpy arrays, and
    other common types. Falls back to ``id(v)`` for truly opaque objects.
    """
    return hash(_to_hashable(v))


def _to_hashable(v: Any) -> Any:
    """Recursively convert a value to a hashable representation.

    - ``dict``  -> sorted tuple of ``(key, hashable_value)`` pairs
    - ``list``  -> tuple of hashable values
    - ``set``   -> frozenset
    - ``tuple`` -> tuple of hashable values (in case it contains unhashable items)
    - Objects with ``tobytes``/``shape``/``dtype`` (numpy arrays) -> ``(shape, dtype, bytes)``
    - Already-hashable scalars (str, int, float, bool, None) -> unchanged
    - Anything else -> ``id(v)`` (object identity)
    """
    if v is None or isinstance(v, (str, int, float, bool, bytes)):
        return v
    if isinstance(v, dict):
        return tuple(sorted((k, _to_hashable(val)) for k, val in v.items()))
    if isinstance(v, list):
        return tuple(_to_hashable(item) for item in v)
    if isinstance(v, set):
        return frozenset(_to_hashable(item) for item in v)
    if isinstance(v, tuple):
        return tuple(_to_hashable(item) for item in v)
    # numpy arrays and similar — hash by content
    if hasattr(v, "tobytes") and hasattr(v, "shape") and hasattr(v, "dtype"):
        return (v.shape, str(v.dtype), v.tobytes())
    # Fallback: try hash, else use identity
    try:
        hash(v)
        return v
    except TypeError:
        return id(v)
