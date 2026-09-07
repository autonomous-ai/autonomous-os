"""Face recognition processor — v2 pipeline (SCRFD + ONNX landmark + EdgeFace).

This replaced the original ``facerecognizer.py``, which has since been DELETED
along with its ``insightface`` dependency — don't go looking for it. The public
API was kept byte-for-byte identical across that migration: same
``FaceRecognizer`` / ``FacePerception`` class names, constructor signatures,
method signatures and return types. Only the internal detection + recognition
models changed:

    old (deleted):            insightface buffalo_sc  (SCRFD det + ArcFace rec)
    new (this pipeline):      SCRFD  -> detection      (bbox + 5 kps + score)
                              MediaPipe landmark ONNX -> alignment (112x112 crop)
                              EdgeFace ONNX -> recognition (embedding)

The pipeline was originally a single ~2.6k-line module; it is split here into
focused submodules for maintainability, with the public surface re-exported so
existing imports (``from ...processors.facerecognizer_v2 import FacePerception``)
keep working unchanged:

    geometry.py     — similarity-transform warp/crop helpers
    landmark.py     — ONNX MediaPipe FaceMesh landmark regressor + aligner
    scrfd.py        — SCRFD face detector
    edgeface.py     — EdgeFace embedder
    pipeline.py     — SCRFD -> landmark -> EdgeFace ``_EdgeFacePipeline``
    recognizer.py   — ``FaceRecognizer`` (enrollment bank, retrieval, extend)
    perception.py   — ``FacePerception`` (presence state machine)
    model_store.py  — model paths + download-on-first-use from cloud storage
    constants.py    — shared on-disk paths / thresholds

The ONNX model weights are NOT committed to the repo. They are fetched on first
use into ``/root/local/models`` (same convention as ``rtmpose-m.onnx``) from the
public weights bucket — see ``model_store.py``.
"""

from .constants import USERS_DIR
from .perception import FacePerception
from .recognizer import FaceRecognizer

__all__ = ["FacePerception", "FaceRecognizer", "USERS_DIR"]
