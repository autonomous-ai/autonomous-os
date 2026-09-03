"""Per-detection debug capture for the v2 face pipeline.

Every face the recognizer decides on writes its OWN timestamped folder under
``config.FACEID_LOG_DIR``, so a false acceptance (wrong person matched) or a
false rejection can be eyeballed against the actual frame instead of guessed at
from a log line. Same folder shape and naming scheme as the face-emotion debug
log, so both can be reviewed with the same tooling.

Knobs live in ``hal/config.py`` with the rest of the HAL environment:
``HAL_FACEID_DEBUG_LOG_ENABLED`` / ``HAL_FACEID_LOG_DIR`` /
``HAL_FACEID_LOG_MAX_TRIGGERS``.

Note this deliberately does NOT write a folder when the detector finds no face
at all: unlike emotion (which only runs once a face exists), face detection ticks
every ``HAL_SENSING_INTERVAL`` seconds whether or not anybody is in the room, so
empty-room frames would evict every interesting detection from the capped
directory within minutes.
"""

import json
import logging
import os
import shutil
import threading
import time
from typing import Any

import cv2

logger = logging.getLogger(__name__)


class FaceIdDebugLogger:
    """Persists every face-recognition decision to its OWN timestamped folder
    under ``config.FACEID_LOG_DIR``.

    The folder name encodes the verdict up front so a wrong identity is obvious
    from the directory listing alone:

      - decision: ``<timestamp>_<face_id>_<similarity>`` — e.g.
        ``20260903-160102-091365_long_0.62``
      - failure:  ``<timestamp>_FAIL-<reason>`` — e.g.
        ``20260903-160104-226340_FAIL-too-small``

    Each folder contains:

      - ``input.jpg``     the face cut out of the original frame (detector bbox)
      - ``aligned.jpg``   the 112x112 aligned crop actually fed to EdgeFace
      - ``frame.jpg``     the clean, unannotated original frame
      - ``annotated.jpg`` full frame with bbox + "<id> <similarity>" drawn
      - ``result.json``   identity, every bank's similarity, thresholds, bbox

    ``result.json`` records both the raw ``bbox`` and the clamped ``crop_box``
    actually used, so ``input.jpg`` can be reproduced from ``frame.jpg`` (or
    re-cropped with different padding) offline.

    All writes are best-effort and lock-guarded — a logging failure never breaks
    detection. When ``max_triggers > 0`` the oldest folders are pruned so the log
    directory stays bounded.
    """

    def __init__(self, root_dir: str, enabled: bool = True, max_triggers: int = 0):
        self._root: str = root_dir
        self._enabled: bool = enabled
        self._max_triggers: int = max_triggers
        self._lock: threading.Lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _slug(text: Any) -> str:
        """Filesystem-safe token for a folder name."""
        s = "".join(c if c.isalnum() else "-" for c in str(text)).strip("-")
        return s or "unknown"

    @staticmethod
    def _json_default(o: Any) -> Any:
        """Coerce non-JSON types (numpy scalars/arrays) to native values so
        similarities serialize as numbers, not stringified reprs."""
        if hasattr(o, "item"):  # numpy scalar → python int/float
            return o.item()
        if hasattr(o, "tolist"):  # numpy array → list
            return o.tolist()
        return str(o)

    def _annotate(
        self,
        frame: cv2.typing.MatLike,
        bbox: list[int],
        label: str,
        similarity: float,
        color: tuple[int, int, int],
    ) -> cv2.typing.MatLike | None:
        try:
            vis = frame.copy()
            x1, y1, x2, y2 = bbox
            _ = cv2.rectangle(vis, (x1, y1), (x2, y2), color, 2)
            _ = cv2.putText(
                vis,
                f"{label} {similarity:.2f}",
                (x1, max(0, y1 - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                color,
                2,
            )
            return vis
        except Exception:
            return None

    def _write_folder(
        self,
        name: str,
        record: dict[str, Any],
        images: dict[str, cv2.typing.MatLike | None],
    ) -> str | None:
        if not self._enabled:
            return None
        try:
            with self._lock:
                now = time.time()
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + (
                    "-%06d" % int((now % 1) * 1_000_000)
                )
                folder = os.path.join(self._root, f"{stamp}_{name}")
                os.makedirs(folder, exist_ok=True)
                for filename, image in images.items():
                    if image is not None:
                        _ = cv2.imwrite(os.path.join(folder, filename), image)
                full: dict[str, Any] = {"timestamp": stamp, "ts": now}
                full.update(record)
                # Serialize FIRST — a serialization error (e.g. a circular ref)
                # then raises before the file is opened, so result.json is never
                # left half-written/truncated.
                text = json.dumps(full, indent=2, default=self._json_default)
                with open(
                    os.path.join(folder, "result.json"), "w", encoding="utf-8"
                ) as f:
                    _ = f.write(text)
                self._prune()
            return folder
        except Exception as e:  # never let debug logging break detection
            logger.debug("[face] debug save failed: %s", e)
            return None

    def _prune(self) -> None:
        """Drop oldest detection folders past the cap. Caller holds the lock."""
        if self._max_triggers <= 0:
            return
        try:
            entries = [
                e
                for e in os.listdir(self._root)
                if os.path.isdir(os.path.join(self._root, e))
            ]
            # Names are timestamp-prefixed → lexical sort == chronological.
            entries.sort()
            for stale in entries[: max(0, len(entries) - self._max_triggers)]:
                shutil.rmtree(os.path.join(self._root, stale), ignore_errors=True)
        except Exception as e:
            logger.debug("[face] debug prune failed: %s", e)

    def save_decision(
        self,
        face_id: str,
        similarity: float,
        face_crop: cv2.typing.MatLike | None = None,
        aligned: cv2.typing.MatLike | None = None,
        frame: cv2.typing.MatLike | None = None,
        bbox: list[int] | None = None,
        color: tuple[int, int, int] = (0, 255, 0),
        **meta: Any,
    ) -> str | None:
        """One face the recognizer reached an identity decision on → its own
        folder, named ``<timestamp>_<face_id>_<similarity>``."""
        annotated = (
            self._annotate(frame, bbox, face_id, similarity, color)
            if frame is not None and bbox is not None
            else None
        )
        name = f"{self._slug(face_id)}_{similarity:.2f}"
        record: dict[str, Any] = {
            "status": "decision",
            "face_id": face_id,
            "similarity": similarity,
        }
        if bbox is not None:
            record["bbox"] = list(bbox)
        record.update(meta)
        # frame.jpg is the clean original → re-crop from it using bbox/crop_box.
        return self._write_folder(
            name,
            record,
            {
                "input.jpg": face_crop,
                "aligned.jpg": aligned,
                "frame.jpg": frame,
                "annotated.jpg": annotated,
            },
        )

    def save_failure(
        self,
        reason: str,
        face_crop: cv2.typing.MatLike | None = None,
        aligned: cv2.typing.MatLike | None = None,
        frame: cv2.typing.MatLike | None = None,
        **meta: Any,
    ) -> str | None:
        """One face that never reached an identity decision → its own folder,
        with the input image (when available) and the reason it was dropped."""
        name = "FAIL-" + self._slug(reason)
        record: dict[str, Any] = {"status": "failure", "reason": reason}
        record.update(meta)
        return self._write_folder(
            name,
            record,
            {"input.jpg": face_crop, "aligned.jpg": aligned, "frame.jpg": frame},
        )
