"""The face-ID debug capture must cost nothing when it is switched off.

It writes six files per face per sensing tick and exists to investigate
recognition bugs, so it ships disabled. The property worth locking down is not
"it writes nothing" — that is obvious — but that it returns BEFORE copying the
frame. Annotating copies a 1280x720 frame twice per face, which at a 2s tick is
pure waste on every production device.
"""

import numpy as np
import pytest

from hal.drivers.sensing.perceptions.processors.faceid.debug_log import (
    FaceIdDebugLogger,
)


@pytest.fixture
def frame() -> np.ndarray:
    return np.zeros((720, 1280, 3), np.uint8)


def _logger(tmp_path, enabled: bool) -> FaceIdDebugLogger:
    return FaceIdDebugLogger(root_dir=str(tmp_path / "logs"), enabled=enabled)


def test_disabled_writes_nothing(tmp_path, frame):
    log = _logger(tmp_path, enabled=False)
    assert log.save_decision("long", 0.62, frame=frame, bbox=[10, 10, 110, 150]) is None
    assert log.save_failure("truncated", frame=frame, bbox=[10, 10, 110, 150]) is None
    assert not (tmp_path / "logs").exists()


def test_disabled_does_not_copy_the_frame(tmp_path, frame, monkeypatch):
    """The whole point: bail before _annotate / _draw_landmarks, not inside
    _write_folder. Both helpers copy the frame, so reaching them at all is the
    regression this guards against."""
    calls: list[str] = []
    monkeypatch.setattr(
        FaceIdDebugLogger, "_annotate",
        lambda *a, **k: calls.append("annotate"),
    )
    monkeypatch.setattr(
        FaceIdDebugLogger, "_draw_landmarks",
        lambda *a, **k: calls.append("landmarks"),
    )

    log = _logger(tmp_path, enabled=False)
    _ = log.save_decision(
        "long", 0.62, frame=frame, bbox=[10, 10, 110, 150],
        landmarks=np.zeros((468, 2), np.float32),
    )
    _ = log.save_failure(
        "truncated", frame=frame, bbox=[10, 10, 110, 150],
        landmarks=np.zeros((468, 2), np.float32),
    )
    assert calls == []

    # ...and the same helpers DO run once it is switched on, so the assertion
    # above is testing the guard rather than a broken code path.
    _ = _logger(tmp_path, enabled=True).save_decision(
        "long", 0.62, frame=frame, bbox=[10, 10, 110, 150],
        landmarks=np.zeros((468, 2), np.float32),
    )
    assert calls == ["annotate", "landmarks"]


def test_enabled_writes_a_folder_per_face(tmp_path, frame):
    log = _logger(tmp_path, enabled=True)
    folder = log.save_decision(
        "long", 0.62,
        face_crop=frame[10:150, 10:110],
        frame=frame,
        bbox=[10, 10, 110, 150],
        landmarks=np.zeros((468, 2), np.float32),
        kps5=np.zeros((5, 2), np.float32),
    )
    assert folder is not None
    written = {p.name for p in (tmp_path / "logs").iterdir() for p in p.iterdir()}
    assert {"input.jpg", "frame.jpg", "annotated.jpg", "result.json"} <= written
    # the verdict is in the folder name, so a bad match is visible in `ls`
    assert (tmp_path / "logs").iterdir().__next__().name.endswith("_long_0.62")


def test_default_is_off():
    """A device that nobody has configured must not be writing capture folders."""
    import hal.config as config

    assert config.FACEID_DEBUG_LOG_ENABLED is False
