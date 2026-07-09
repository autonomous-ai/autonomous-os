"""Face recognition processor — v2 pipeline (SCRFD + ONNX landmark + EdgeFace).

Drop-in, API-compatible replacement for ``facerecognizer.py``. The PUBLIC API is
byte-for-byte identical: same ``FaceRecognizer`` / ``FacePerception`` class names,
constructor signatures, method signatures and return types. Only the internal
detection + recognition models change:

    old (facerecognizer.py):  insightface buffalo_sc  (SCRFD det + ArcFace rec)
    new (this file):          SCRFD  -> detection      (bbox + 5 kps + score)
                              MediaPipe landmark ONNX -> alignment (112x112 crop)
                              EdgeFace ONNX -> recognition (embedding)

Alignment originally used the pip ``mediapipe`` FaceMesh, but that package cannot
be installed in the deployment environment. It is replaced here by a local ONNX
port of the MediaPipe FaceMesh landmark regressor (``_MediaPipeLandmarkONNX``,
inlined & renamed from ``temp-updated-for-facerecognizer/mediapipe_landmark_onnx.py``).
No ``import mediapipe`` remains.

The swap is localized to the ``FaceRecognizer._app`` object. Everything the rest
of the codebase touches — ``start()``, ``register()``, ``detect()``, ``reset()``,
the ``owners`` / ``strangers`` properties, and the whole ``FacePerception`` state
machine — is unchanged, so no external caller breaks.

This file is SELF-CONTAINED for side-by-side testing against the old pipeline:
  * All helpers ported from ``temp-updated-for-facerecognizer/`` are inlined here
    and renamed (``_v2_*`` funcs, ``_SCRFDDetector`` / ``_MediaPipeLandmarkONNX`` /
    ``_OnnxLandmarkAligner`` / ``_EdgeFaceEmbedder`` / ``_EdgeFacePipeline``
    classes) so they NEVER collide with the sibling ``utils.py`` (which has a
    different, unrelated API). Nothing imports from ``temp-updated-for-facerecognizer``
    at runtime; that folder is reference-only and can be removed.
  * The embedding pipeline exposes ``.get(frame)`` returning the exact dict shape
    insightface produced: ``{'bbox', 'kps', 'det_score', 'embedding'}``.

Model files are resolved from env vars (defaults follow the pose-model convention
in config.py, i.e. ``/root/local/models/``) — NO existing file is modified:
    HAL_FACE_SCRFD_MODEL_PATH     (default /root/local/models/scrfd.onnx)
    HAL_FACE_EDGEFACE_MODEL_PATH  (default /root/local/models/edgeface.onnx)
    HAL_FACE_LANDMARK_MODEL_PATH  (default /root/local/models/mediapipe_landmark.onnx)

Persisted stranger state (embeds.npy / labels.npy) is kept in a SEPARATE ``v2``
subdirectory. EdgeFace embeddings are not comparable with buffalo_sc embeddings,
so sharing the old bank would either mismatch semantically or crash on a
dimension mismatch during the ``embeds @ bank.T`` retrieval. See the "Upgrade
conflicts" notes at the bottom of this file.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
from copy import copy
from pathlib import Path
from typing import Any, Callable, override

import cv2
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
import requests
from numpy.linalg import inv, lstsq, matrix_rank, norm

import hal.config as config
from hal.drivers.sensing.perceptions.models import (
    Face,
    FaceDetectionData,
    PersonData,
    PersonKind,
)
from hal.drivers.sensing.perceptions.typing import SendEventCallable
from hal.drivers.sensing.perceptions.utils import PerceptionStateObservers
from hal.drivers.sensing.presence_service import PresenseService

from .base import Perception

logger = logging.getLogger(__name__)

_NO_MATCH = -2.0  # sentinel score used when an embedding bank is empty

# Model paths (v2). Resolved from env with /root/local/models defaults, mirroring
# POSE_MOTION_MODEL_PATH in config.py. Kept local to this file so config.py stays
# untouched.
_FACE_MODEL_PATH: str = os.environ.get(
    "HAL_FACE_MODEL_PATH", "/opt/hal/drivers/sensing/perceptions/processors/models/"
)

_SCRFD_MODEL_PATH: str = os.environ.get(
    "HAL_FACE_SCRFD_MODEL_PATH", os.path.join(_FACE_MODEL_PATH, "scrfd_2.5g_fp32.onnx")
)
_EDGEFACE_MODEL_PATH: str = os.environ.get(
    "HAL_FACE_EDGEFACE_MODEL_PATH", os.path.join(_FACE_MODEL_PATH, "edgeface_s_gamma_05_opt.onnx")
)
# MediaPipe FaceMesh landmark regressor exported to ONNX (replaces the pip
# `mediapipe` dependency, which cannot be installed on the target device).
_LANDMARK_MODEL_PATH: str = os.environ.get(
    "HAL_FACE_LANDMARK_MODEL_PATH", os.path.join(_FACE_MODEL_PATH, "MediaPipeFaceLandmarkDetector.onnx")
)
# Face-presence probability above which the ONNX landmarks are trusted for
# alignment; below it the detection is dropped (no SCRFD keypoint fallback), so
# this doubles as a false-alarm gate. See _OnnxLandmarkAligner.
_LANDMARK_CONF_THRESHOLD: float = float(
    os.environ.get("HAL_FACE_LANDMARK_CONF_THRESHOLD", "0.6")
)

# Per-user data directory (face photos, wellbeing notes, mood history)
USERS_DIR = Path(config.USERS_DIR)
USERS_DIR.mkdir(parents=True, exist_ok=True)
# v2 keeps its own stranger bank so EdgeFace embeddings never mix with the old
# buffalo_sc embeds.npy (different dimensionality / metric) (removed).
STRANGER_STATE_DIR = Path(config.STRANGERS_DIR) # / "v2"
STRANGER_STATE_DIR.mkdir(exist_ok=True, parents=True)
_STRANGER_STATS_FILE = USERS_DIR / ".stranger_stats.json"
_STRANGER_SNAPSHOTS_DIR = STRANGER_STATE_DIR / "snapshots"

# Visit count at which hal prompts the user to enroll a familiar stranger.
# Fires exactly once per stranger when count first reaches this value; the
# face-enroll skill handles asking the user and POST /face/enroll on confirm.
_FAMILIAR_VISIT_THRESHOLD = 2

# Auto-captured "extended" enrollment views persist in this per-user subfolder,
# i.e. USERS_DIR/<user>/.extended/. It is dot-prefixed so the upload loader
# (which reads image FILES directly under the user dir) never mistakes an
# extended view for an upload, and the photos watcher can skip it (the extended
# set self-manages and must not trigger a full re-embed of every upload). Each
# captured view is stored as a JPEG crop PLUS a sidecar .npy holding its
# (already L2-normalized) embedding, so a restart reloads the exact embedding
# and never has to re-detect a hard side-view — which is the very thing this
# feature exists to keep, and the thing a re-detect would be most likely to miss.
_EXTENDED_SUBDIR = ".extended"
_EXTENDED_IMG_EXT = ".jpg"
_EXTENDED_EMB_EXT = ".npy"
# Image extensions that count as enrollment uploads. ``load_from_disk`` reads
# only files with these suffixes sitting DIRECTLY in a user folder; the photos
# watcher uses the same set so the two agree on what "an enrollment change" is.
_ENROLL_IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}

# =============================================================================
# Alignment helpers — ported & renamed from temp-updated-face-recognizer/utils.py
# (prefixed _v2_ / made module-private to avoid clashing with the sibling utils.py)
# =============================================================================

# ArcFace 5-point reference template used by EdgeFace (112x112).
_V2_REFERENCE_FACIAL_POINTS = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)


class _V2FaceWarpException(Exception):
    def __str__(self) -> str:
        return f"In File {__file__}: {super().__str__()}"


def _v2_tformfwd(trans: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """Apply forward affine transform."""
    uv_h = np.hstack((uv, np.ones((uv.shape[0], 1))))
    xy = uv_h @ trans
    return xy[:, :-1]


def _v2_find_nonreflective_similarity(
    uv: np.ndarray, xy: np.ndarray, options: dict | None = None
):
    """Find non-reflective similarity transform between uv and xy."""
    K = options.get("K", 2) if options else 2
    M = xy.shape[0]

    x, y = xy[:, 0:1], xy[:, 1:2]
    u, v = uv[:, 0:1], uv[:, 1:2]

    X = np.vstack(
        (
            np.hstack((x, y, np.ones((M, 1)), np.zeros((M, 1)))),
            np.hstack((y, -x, np.zeros((M, 1)), np.ones((M, 1)))),
        )
    )
    U = np.vstack((u, v))

    if matrix_rank(X) >= 2 * K:
        r, _, _, _ = lstsq(X, U, rcond=None)
    else:
        raise ValueError("cp2tform:twoUniquePointsReq")

    sc, ss, tx, ty = r.flatten()
    Tinv = np.array([[sc, -ss, 0], [ss, sc, 0], [tx, ty, 1]])
    T = inv(Tinv)
    T[:, 2] = [0, 0, 1]
    return T, Tinv


def _v2_find_similarity(uv: np.ndarray, xy: np.ndarray, options: dict | None = None):
    """Find similarity transform with optional reflection."""
    trans1, trans1_inv = _v2_find_nonreflective_similarity(uv, xy, options)

    xyR = xy.copy()
    xyR[:, 0] *= -1
    trans2r, _ = _v2_find_nonreflective_similarity(uv, xyR, options)

    TreflectY = np.array([[-1, 0, 0], [0, 1, 0], [0, 0, 1]])
    trans2 = trans2r @ TreflectY

    norm1 = norm(_v2_tformfwd(trans1, uv) - xy)
    norm2 = norm(_v2_tformfwd(trans2, uv) - xy)

    return (trans1, trans1_inv) if norm1 <= norm2 else (trans2, inv(trans2))


def _v2_get_similarity_transform_for_cv2(
    src_pts: np.ndarray, dst_pts: np.ndarray, reflective: bool = True
) -> np.ndarray:
    """Get cv2-compatible affine transform matrix."""
    trans, _ = (
        _v2_find_similarity(src_pts, dst_pts)
        if reflective
        else _v2_find_nonreflective_similarity(src_pts, dst_pts)
    )
    return trans[:, :2].T


def _v2_warp_and_crop_face(
    src_img: np.ndarray,
    facial_pts,
    reference_pts: np.ndarray = _V2_REFERENCE_FACIAL_POINTS,
    crop_size: tuple[int, int] = (112, 112),
    scale: float = 1.0,
) -> np.ndarray:
    """Warp and crop face using a similarity transform to the reference template."""
    ref_pts = reference_pts * scale
    ref_pts += np.mean(reference_pts, axis=0) - np.mean(ref_pts, axis=0)

    src_pts = np.array(facial_pts, dtype=np.float32)
    if src_pts.shape != ref_pts.shape:
        raise _V2FaceWarpException(
            "facial_pts and reference_pts must have the same shape"
        )

    tfm = _v2_get_similarity_transform_for_cv2(src_pts, ref_pts)
    return cv2.warpAffine(src_img, tfm, crop_size)


def _v2_landmarks_out_of_bounds(pts5: np.ndarray, bbox, frame_shape) -> bool:
    """True if any of the 5 alignment points falls outside the bbox or image."""
    h, w = frame_shape[:2]
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    x_lo, x_hi = max(0.0, x1), min(float(w), x2)
    y_lo, y_hi = max(0.0, y1), min(float(h), y2)
    xs, ys = pts5[:, 0], pts5[:, 1]
    return bool(
        np.any(xs < x_lo) or np.any(xs > x_hi)
        or np.any(ys < y_lo) or np.any(ys > y_hi)
    )


# =============================================================================
# MediaPipe FaceMesh landmark regressor (ONNX) — ported & renamed from
# temp-updated-for-facerecognizer/mediapipe_landmark_onnx.py
#
# The pip `mediapipe` package cannot be installed on the target device, so its
# FaceMesh landmark stage is served by this ONNX regressor instead. The model is
# the landmark REGRESSOR only (no internal detector): it expects a square,
# roughly-upright face ROI, so `detect_in_frame` builds a square ROI centered on
# the SCRFD bbox (optionally roll-corrected with the eye keypoints), runs the
# regressor, and maps the landmarks back to full-frame coordinates.
#
# Model I/O (verified):
#   input  'image'     : (1, 3, 192, 192) float32, NCHW, RGB, range [0, 1]
#   output 'scores'    : (1,)             float32, face-presence logit
#   output 'landmarks' : (1, 468, 3)      float32, (x, y, z) normalized to [0, 1]
# =============================================================================

# FaceMesh landmark indices -> the 5 canonical alignment points (same indices the
# old pip-MediaPipe path used).
_V2_LM_NOSE = 1
_V2_LM_MOUTH_RIGHT = 287
_V2_LM_MOUTH_LEFT = 57
_V2_LM_RIGHT_EYE = (362, 263)  # corners -> averaged to the eye center
_V2_LM_LEFT_EYE = (33, 243)    # corners -> averaged to the eye center


class _MediaPipeLandmarkONNX:
    """ONNX MediaPipe FaceMesh landmark regressor (replaces pip mediapipe)."""

    def __init__(
        self,
        model_path: str,
        input_size=192,
        conf_thresh: float = 0.6,
        roi_scale: float = 1.4,
        roll_align: bool = True,
        fp16: bool = False,
        session_options: ort.SessionOptions | None = None,
    ):
        """
        model_path: path to the MediaPipe landmark ONNX model.
        input_size: model input resolution (int or (w, h)); MediaPipe = 192.
        conf_thresh: face-presence probability (sigmoid of the raw 'scores'
            output) above which the landmarks are trusted. ~0.6 mirrors the pip
            MediaPipe succeed/fail split — clear frontal faces use the dense
            landmarks; hard/profile / non-face crops fall below it and are
            dropped (no keypoint fallback).
        roi_scale: square ROI side as a multiple of the bbox's longer side. 1.4
            reproduces the old path (FaceMesh on bbox + 0.2 margin ≈ 1.4x).
        roll_align: if True and eye keypoints are given, rotate the ROI so the
            eyes are horizontal before inference (mirrors MediaPipe).
        fp16: cast the input blob to float16 (use with an fp16 exported model).
        """
        sess_opts = session_options or ort.SessionOptions()
        self.session = ort.InferenceSession(model_path, sess_opts)
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [o.name for o in self.session.get_outputs()]

        self.name = os.path.basename(model_path).rsplit(".", 1)[0]
        if isinstance(input_size, int):
            self.input_size = (input_size, input_size)
        else:
            self.input_size = input_size

        self.conf_thresh = conf_thresh
        self.roi_scale = roi_scale
        self.roll_align = roll_align
        self.fp16 = fp16

    def _blob(self, bgr_crop: np.ndarray) -> np.ndarray:
        """BGR uint8 crop -> (1,3,H,W) RGB blob in [0,1] (MediaPipe order)."""
        img = cv2.resize(bgr_crop, self.input_size)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(img, (2, 0, 1))[None]
        return blob.astype(np.float16 if self.fp16 else np.float32)

    def _run(self, bgr_crop: np.ndarray):
        """Run the session on a crop. Returns (xy_in_input_px, score)."""
        scores, landmarks = self.session.run(
            self.output_names, {self.input_name: self._blob(bgr_crop)}
        )
        score = float(scores[0]) # already sigmoided in the model output (face-presence probability)
        # normalized [0,1] -> pixels of the (square) model input
        xy = landmarks[0][:, :2].astype(np.float32) * np.float32(self.input_size)
        return xy, score

    def _roi_transform(self, bbox, kps=None):
        """Affine (2x3) mapping the frame into a square input_size ROI centered
        on the bbox, optionally roll-corrected using the eye keypoints."""
        x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        side = max(x2 - x1, y2 - y1) * self.roi_scale

        angle = 0.0
        if self.roll_align and kps is not None:
            reye, leye = np.asarray(kps)[0], np.asarray(kps)[1]
            angle = float(
                np.degrees(np.arctan2(leye[1] - reye[1], leye[0] - reye[0]))
            )

        out = self.input_size[0]
        M = cv2.getRotationMatrix2D((cx, cy), angle, out / side)
        M[0, 2] += out / 2.0 - cx
        M[1, 2] += out / 2.0 - cy
        return M

    def detect_in_frame(self, frame: np.ndarray, bbox, kps=None):
        """Detect 468 landmarks for one face, in FULL-FRAME pixel coords.

        Returns (landmarks_frame (468,2) float32, score in [0,1]).
        """
        M = self._roi_transform(bbox, kps)
        roi = cv2.warpAffine(frame, M, self.input_size)
        xy, score = self._run(roi)
        Minv = cv2.invertAffineTransform(M)
        pts = np.hstack([xy, np.ones((xy.shape[0], 1), np.float32)])
        xy_frame = (pts @ Minv.T).astype(np.float32)
        return xy_frame, score

    @staticmethod
    def to_5points(landmarks: np.ndarray) -> np.ndarray:
        """Reduce the 468 dense landmarks to the 5 canonical points in the order
        the ArcFace template expects: [reye, leye, nose, mouthright, mouthleft]."""
        lm = np.asarray(landmarks, dtype=np.float32)
        reye = lm[list(_V2_LM_RIGHT_EYE)].mean(axis=0)
        leye = lm[list(_V2_LM_LEFT_EYE)].mean(axis=0)
        return np.stack(
            [reye, leye, lm[_V2_LM_NOSE], lm[_V2_LM_MOUTH_RIGHT], lm[_V2_LM_MOUTH_LEFT]],
            axis=0,
        ).astype(np.float32)


class _OnnxLandmarkAligner:
    """ONNX-landmark alignment (drop-in for the old MediaPipe FaceMesh aligner).

    Only confident, in-bounds dense landmarks are used to align:
        score >= conf_thresh -> align from the dense ONNX landmarks
        score <  conf_thresh -> drop the face (no embedding)
    There is NO SCRFD keypoint fallback, so low-confidence / non-face detections
    are gated out instead of being force-aligned and embedded.
    """

    def __init__(self, landmarker: _MediaPipeLandmarkONNX) -> None:
        self._landmarker = landmarker

    def align_crop_from_bbox(self, frame: np.ndarray, bbox, kps=None):
        """Aligned 112x112 crop for one detection.

        Returns ``(aligned, pts5, score)``:
            * ``aligned``: 112x112 BGR crop, or None if the face cannot align.
            * ``pts5``: the 5 alignment points used for the warp, or None.

        Faces whose landmark confidence is below ``conf_thresh`` (or whose
        landmarks fall outside the bbox/image) are dropped: there is NO SCRFD
        keypoint fallback, so low-confidence / non-face detections never produce
        an embedding. ``kps`` is still used by the landmarker for ROI roll
        correction.
        """
        try:
            landmarks, score = self._landmarker.detect_in_frame(frame, bbox, kps=kps)
        except Exception as e:  # noqa: BLE001
            logger.debug("[face-v2] landmark inference error: %s", e)
            return None, None

        if landmarks is not None and score >= self._landmarker.conf_thresh:
            pts5 = self._landmarker.to_5points(landmarks)
            if not _v2_landmarks_out_of_bounds(pts5, bbox, frame.shape):
                try:
                    return _v2_warp_and_crop_face(frame, pts5), pts5
                except Exception as e:  # noqa: BLE001
                    logger.debug("[face-v2] landmark alignment error: %s", e)

        return None, None


# =============================================================================
# SCRFD detector — ported & renamed from temp-updated-face-recognizer/scrfd_onnx.py
# =============================================================================


class _SCRFDDetector:
    def __init__(
        self,
        model_path: str,
        input_size=640,
        confidence_threshold: float = 0.35,
        nms_threshold: float = 0.4,
        fp16: bool = False,
        max_num: int = 0,
        metric: str = "default",
        session_options: ort.SessionOptions | None = None,
    ):
        sess_opts = session_options or ort.SessionOptions()
        self.session = ort.InferenceSession(model_path, sess_opts)
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [o.name for o in self.session.get_outputs()]

        self.name = os.path.basename(model_path).rsplit(".", 1)[0]
        if isinstance(input_size, int):
            self.input_size = (input_size, input_size)
        else:
            self.input_size = input_size

        self.conf_thresh = confidence_threshold
        self.nms_thresh = nms_threshold
        self.fp16 = fp16
        self.max_num = max_num
        self.metric = metric

        onnx_out_num = len(self.output_names)
        self.use_kps = onnx_out_num in (9, 15)
        self.fmc = 5 if onnx_out_num >= 10 else 3
        self.num_anchors = 1 if onnx_out_num in (10, 15) else 2
        self.feat_stride_fpn = (
            [8, 16, 32, 64, 128] if self.fmc == 5 else [8, 16, 32]
        )

    @staticmethod
    def distance2bbox(points, distance, max_shape=None):
        x1 = points[:, 0] - distance[:, 0]
        y1 = points[:, 1] - distance[:, 1]
        x2 = points[:, 0] + distance[:, 2]
        y2 = points[:, 1] + distance[:, 3]
        if max_shape is not None:
            x1 = np.clip(x1, 0, max_shape[1])
            y1 = np.clip(y1, 0, max_shape[0])
            x2 = np.clip(x2, 0, max_shape[1])
            y2 = np.clip(y2, 0, max_shape[0])
        return np.stack([x1, y1, x2, y2], axis=-1)

    @staticmethod
    def distance2kps(points, distance, max_shape=None):
        preds = []
        for i in range(0, distance.shape[1], 2):
            px = points[:, i % 2] + distance[:, i]
            py = points[:, i % 2 + 1] + distance[:, i + 1]
            if max_shape is not None:
                px = np.clip(px, 0, max_shape[1])
                py = np.clip(py, 0, max_shape[0])
            preds.append(px)
            preds.append(py)
        return np.stack(preds, axis=-1)

    @staticmethod
    def preprocess_image(image, input_size):
        h0, w0 = image.shape[:2]
        w_in, h_in = input_size
        scale = min(w_in / w0, h_in / h0)
        nw, nh = int(w0 * scale), int(h0 * scale)
        img_resized = cv2.resize(image, (nw, nh))
        pad_w = (w_in - nw) // 2
        pad_h = (h_in - nh) // 2
        det_img = np.full((h_in, w_in, 3), 128, dtype=np.uint8)
        det_img[pad_h:pad_h + nh, pad_w:pad_w + nw] = img_resized
        return det_img, scale, (pad_w, pad_h)

    def decode_and_filter(self, net_outs, thresh, input_shape):
        scores_list = []
        bboxes_list = []
        kps_list = []
        center_cache: dict = {}
        _, _, h_in, w_in = input_shape
        for idx, stride in enumerate(self.feat_stride_fpn):
            scores = net_outs[idx][0]
            boxes = net_outs[idx + self.fmc][0] * stride
            if self.use_kps:
                kps = net_outs[idx + self.fmc * 2][0] * stride
            fh, fw = h_in // stride, w_in // stride

            key = (fh, fw, stride)
            if key in center_cache:
                anchor_centers = center_cache[key]
            else:
                grid = np.stack(
                    np.mgrid[:fh, :fw][::-1], axis=-1
                ).astype(np.float32)
                pts = (grid * stride).reshape(-1, 2)
                if self.num_anchors > 1:
                    pts = np.stack([pts] * self.num_anchors, 1).reshape(-1, 2)
                if len(center_cache) < 100:
                    center_cache[key] = pts
                anchor_centers = pts

            idxs = np.where(scores >= thresh)[0]
            if idxs.size == 0:
                continue
            bboxes = self.distance2bbox(anchor_centers, boxes)
            scores_list.append(scores[idxs])
            bboxes_list.append(bboxes[idxs])

            if self.use_kps:
                kpss = self.distance2kps(anchor_centers, kps)
                kps_list.append(kpss[idxs])
        return scores_list, bboxes_list, kps_list

    @staticmethod
    def nms(dets, thresh):
        x1, y1, x2, y2, s = (
            dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
        )
        areas = (x2 - x1 + 1) * (y2 - y1 + 1)
        order = s.argsort()[::-1]
        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(i)
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            w = np.maximum(0, xx2 - xx1 + 1)
            h = np.maximum(0, yy2 - yy1 + 1)
            inter = w * h
            ovr = inter / (areas[i] + areas[order[1:]] - inter)
            inds = np.where(ovr <= thresh)[0]
            order = order[inds + 1]
        return keep

    def postprocess(self, scores_list, bboxes_list, kps_list, det_scale, pad,
                    original_shape=None):
        if not scores_list:
            empty_kps = (
                np.zeros((0, 5, 2), dtype=np.float32) if self.use_kps else None
            )
            return np.zeros((0, 5), dtype=np.float32), empty_kps
        pad_w, pad_h = pad
        scores = np.vstack(scores_list).ravel()
        bboxes = np.vstack(bboxes_list)
        bboxes -= np.array([pad_w, pad_h, pad_w, pad_h])
        bboxes /= det_scale
        det = np.hstack((bboxes, scores[:, None])).astype(np.float32)

        if self.use_kps:
            kpss = np.vstack(kps_list).reshape((-1, 5, 2))
            kpss -= np.array([[pad_w, pad_h]])
            kpss /= det_scale
        else:
            kpss = None

        order = det[:, 4].argsort()[::-1]
        det = det[order]
        if kpss is not None:
            kpss = kpss[order]

        keep = self.nms(det, self.nms_thresh)
        det = det[keep]
        if kpss is not None:
            kpss = kpss[keep]

        if 0 < self.max_num < det.shape[0] and original_shape is not None:
            area = (det[:, 2] - det[:, 0]) * (det[:, 3] - det[:, 1])
            if self.metric == "max":
                vals = area
            else:
                cy, cx = original_shape[0] // 2, original_shape[1] // 2
                centers = np.stack(
                    [
                        (det[:, 0] + det[:, 2]) / 2 - cx,
                        (det[:, 1] + det[:, 3]) / 2 - cy,
                    ],
                    axis=1,
                )
                dist2 = np.sum(centers ** 2, axis=1)
                vals = area - dist2 * 2.0
            idxs = np.argsort(vals)[::-1][:self.max_num]
            det = det[idxs]
            if kpss is not None:
                kpss = kpss[idxs]
        return det, kpss

    def detect(self, image: np.ndarray):
        orig_h, orig_w = image.shape[:2]
        det_img, det_scale, pad = self.preprocess_image(image, self.input_size)
        blob = cv2.dnn.blobFromImage(
            det_img,
            scalefactor=1.0 / 128,
            size=self.input_size,
            mean=(127.5, 127.5, 127.5),
            swapRB=True,
        )
        if self.fp16:
            blob = blob.astype(np.float16)
        outs = self.session.run(self.output_names, {self.input_name: blob})
        scores_list, bboxes_list, kps_list = self.decode_and_filter(
            outs, self.conf_thresh, blob.shape
        )
        return self.postprocess(
            scores_list, bboxes_list, kps_list, det_scale, pad,
            original_shape=(orig_h, orig_w),
        )

    def infer(self, image: np.ndarray) -> list:
        det, kpss = self.detect(image)
        faces = []
        for i in range(det.shape[0]):
            faces.append(
                {
                    "bbox": det[i, :4].astype(np.float32),
                    "kps": kpss[i].astype(np.float32) if kpss is not None else None,
                    "det_score": np.float32(det[i, 4]),
                }
            )
        return faces


# =============================================================================
# EdgeFace embedder — ported & renamed from
# temp-updated-face-recognizer/edgeface_onnx.py
# =============================================================================


class _EdgeFaceEmbedder:
    def __init__(
        self,
        model_path: str,
        input_size=112,
        fp16: bool = False,
        l2_normalize: bool = False,
        session_options: ort.SessionOptions | None = None,
    ):
        sess_opts = session_options or ort.SessionOptions()
        self.session = ort.InferenceSession(model_path, sess_opts)
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [o.name for o in self.session.get_outputs()]

        self.name = os.path.basename(model_path).rsplit(".", 1)[0]
        if isinstance(input_size, int):
            self.input_size = (input_size, input_size)
        else:
            self.input_size = input_size

        self.fp16 = fp16
        self.l2_normalize = l2_normalize
        self.mean = 0.5
        self.std = 0.5

    def preprocess(self, aligned_face: np.ndarray) -> np.ndarray:
        img = aligned_face
        if img.shape[1] != self.input_size[0] or img.shape[0] != self.input_size[1]:
            img = cv2.resize(img, self.input_size)
        img = img.astype(np.float32) / 255.0
        img = (img - self.mean) / self.std
        blob = np.transpose(img, (2, 0, 1))[None]
        return blob.astype(np.float16 if self.fp16 else np.float32)

    def _postprocess(self, embeddings: np.ndarray) -> np.ndarray:
        embeddings = embeddings.astype(np.float32)
        if self.l2_normalize:
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            embeddings = embeddings / (norms + 1e-10)
        return embeddings

    def get_embedding(self, aligned_face: np.ndarray) -> np.ndarray:
        blob = self.preprocess(aligned_face)
        out = self.session.run(self.output_names, {self.input_name: blob})[0]
        return self._postprocess(out)[0]


# =============================================================================
# Embedding pipeline — renamed from the reference `FaceRecognizer` so it does NOT
# shadow the public FaceRecognizer below. Exposes `.get(frame)` with the exact
# insightface FaceAnalysis output shape.
# =============================================================================


class _EdgeFacePipeline:
    """SCRFD (detect) -> ONNX landmark (align) -> EdgeFace (embed).

    ``get(frame)`` returns one dict per face, drop-in compatible with
    ``insightface.app.FaceAnalysis.get``:
        {'bbox': float32[4], 'kps': float32[5,2]|None,
         'det_score': np.float32, 'embedding': float32[D]}
    Faces that cannot be aligned are skipped (same as the reference).
    """

    def __init__(
        self,
        scrfd_model_path: str,
        edgeface_model_path: str,
        landmark_model_path: str,
        det_size=640,
        conf_thresh: float = 0.35,
        nms_thresh: float = 0.4,
        det_fp16: bool = False,
        emb_fp16: bool = False,
        landmark_fp16: bool = False,
        landmark_conf_thresh: float = _LANDMARK_CONF_THRESHOLD,
        roi_scale: float = 1.4,
        roll_align: bool = True,
        l2_normalize: bool = False,
        session_options: ort.SessionOptions | None = None,
    ):
        self.detector = _SCRFDDetector(
            scrfd_model_path,
            input_size=det_size,
            confidence_threshold=conf_thresh,
            nms_threshold=nms_thresh,
            fp16=det_fp16,
            session_options=session_options,
        )
        self.landmarker = _MediaPipeLandmarkONNX(
            landmark_model_path,
            conf_thresh=landmark_conf_thresh,
            roi_scale=roi_scale,
            roll_align=roll_align,
            fp16=landmark_fp16,
            session_options=session_options,
        )
        self.aligner = _OnnxLandmarkAligner(self.landmarker)
        self.embedder = _EdgeFaceEmbedder(
            edgeface_model_path,
            fp16=emb_fp16,
            l2_normalize=l2_normalize,
            session_options=session_options,
        )

    def get(self, frame: np.ndarray) -> list:
        detections = self.detector.infer(frame)
        results = []
        for det in detections:
            bbox = det["bbox"]
            kps = det["kps"]

            aligned, kps = self.aligner.align_crop_from_bbox(
                frame, bbox, kps=kps
            )
            if aligned is None:
                continue

            embedding = self.embedder.get_embedding(aligned)
            results.append(
                {
                    "bbox": bbox.astype(np.float32),
                    "kps": None if kps is None else kps.astype(np.float32),
                    "det_score": np.float32(det["det_score"]),
                    "embedding": embedding.astype(np.float32),
                }
            )
        return results

    __call__ = get


class FaceRecognizer:
    FRIEND_PREFIX: str = "friend_"
    STRANGER_PREFIX: str = "stranger_"

    def __init__(
        self,
        area_ratio_threshold: float = config.FACE_AREA_RATIO_THRESHOLD,
        threshold: float = 0.3,
        negative_threshold: float | None = 0.2,
        max_strangers: int = 50,
        scrfd_model_path: str = _SCRFD_MODEL_PATH,
        edgeface_model_path: str = _EDGEFACE_MODEL_PATH,
        landmark_model_path: str = _LANDMARK_MODEL_PATH,
        max_extended_images: int = 5,
        diversity_threshold: float = 0.7,
    ):
        self._area_ratio_threshold: float = area_ratio_threshold
        self._threshold: float = threshold
        self._negative_threshold: float | None = negative_threshold
        self._max_strangers: int = max_strangers
        self._scrfd_model_path: str = scrfd_model_path
        self._edgeface_model_path: str = edgeface_model_path
        self._landmark_model_path: str = landmark_model_path

        # --- Auto-extend enrollment config -----------------------------------
        # Max number of dynamically-captured extra views KEPT per user (on top
        # of their untouched uploaded enrollment images).
        self._max_extended_images: int = max_extended_images
        # A confidently-matched live frame is only added to a user's extended
        # set when its max cosine similarity to that user's existing views
        # (uploads + current extended) is BELOW this value. Anything above is
        # redundant (near-duplicate of a view we already have) and skipped, so
        # the extended set fills up with genuinely new poses (e.g. side-view).
        self._diversity_threshold: float = diversity_threshold

        self._app: _EdgeFacePipeline | None = None
        self._owner_embeddings: npt.NDArray[np.float32] | None = None
        self._owner_labels: npt.NDArray[np.str_] | None = None
        # Dynamically-grown per-user "extended" bank. Same FRIEND_PREFIX labels
        # as the uploads so retrieval maps a match straight back to the friend
        # id regardless of which bank it came from. Kept SEPARATE from
        # ``_owner_embeddings`` so the user's uploads are never mutated and can
        # always be rebuilt verbatim from disk. Mirrored to disk under each
        # user's ``.extended`` folder so it survives restarts (see
        # _EXTENDED_SUBDIR). ``_extended_paths`` runs parallel to the
        # embeddings/labels and holds each view's on-disk JPEG path, so an
        # eviction during pruning can delete the backing files too.
        self._extended_embeddings: npt.NDArray[np.float32] | None = None
        self._extended_labels: npt.NDArray[np.str_] | None = None
        self._extended_paths: npt.NDArray[np.object_] | None = None
        # Monotonic counter appended to extended-view filenames so two views
        # captured in the same millisecond never collide (which would overwrite
        # a file and desync disk from memory).
        self._extended_save_seq: int = 0
        self._stranger_counter: int = 0
        self._stranger_embeddings: npt.NDArray[np.float32] | None = None
        self._stranger_labels: npt.NDArray[np.str_] | None = None

        self._lock: threading.RLock = threading.RLock()
        self._running: bool = False
        self._logger: logging.Logger = logging.getLogger(self.__class__.__name__)

    @property
    def owners(self) -> list[str]:
        with self._lock:
            if self._owner_labels is None:
                return []
            unique: set[str] = set()
            for lbl in self._owner_labels:
                s = str(lbl)
                unique.add(s.removeprefix(self.FRIEND_PREFIX))
            return list(unique)

    @property
    def strangers(self) -> list[str]:
        with self._lock:
            if self._stranger_labels is None:
                return []
            unique: set[str] = set()
            for lbl in self._stranger_labels:
                s = str(lbl)
                unique.add(s.removeprefix(self.STRANGER_PREFIX))
            return list(unique)

    def start(self):
        if self._running:
            self._logger.info(
                "[%s] service has been already started", self.__class__.__name__
            )
            return

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = 1
        sess_opts.inter_op_num_threads = 1

        self._app = _EdgeFacePipeline(
            scrfd_model_path=self._scrfd_model_path,
            edgeface_model_path=self._edgeface_model_path,
            landmark_model_path=self._landmark_model_path,
            l2_normalize=False,
            session_options=sess_opts,
        )
        self._running = True

    def reset(self, owners: bool = True, strangers: bool = True):
        with self._lock:
            if owners:
                self._owner_embeddings = None
                self._owner_labels = None
                # Extended views belong to the (now-cleared) uploads; drop the
                # in-memory copy so no sample dangles on a removed user. The
                # on-disk .extended files are left intact (persistence): a
                # subsequent load_from_disk repopulates them, and only removing a
                # person/photo (which rmtrees the user dir) erases them for good.
                self._extended_embeddings = None
                self._extended_labels = None
                self._extended_paths = None

            if strangers:
                self._stranger_embeddings = None
                self._stranger_labels = None
                self._stranger_counter = 0

    def register(
        self,
        images: list[cv2.typing.MatLike],
        labels: list[str],
    ) -> None:
        if self._app is None:
            msg = f"[{self.__class__.__name__}] service must be started first"
            raise RuntimeError(msg)

        prefixed_labels = [self.FRIEND_PREFIX + str(lbl) for lbl in labels]
        new_embeddings = []
        new_labels = []
        for image, label in zip(images, prefixed_labels):
            results = self._app.get(image)
            for r in results:
                emb = r["embedding"]
                new_embeddings.append(emb / np.linalg.norm(emb))
                new_labels.append(label)

        if new_embeddings:
            stacked_e = np.stack(new_embeddings, axis=0)
            stacked_l = np.stack(new_labels, axis=0)

            with self._lock:
                self._owner_embeddings = (
                    np.concatenate([self._owner_embeddings, stacked_e])
                    if self._owner_embeddings is not None
                    else stacked_e
                )
                self._owner_labels = (
                    np.concatenate([self._owner_labels, stacked_l])
                    if self._owner_labels is not None
                    else stacked_l
                )
                logger.info(
                    "Added %d faces — total enrolled: %d, total strangers: %d",
                    len(new_embeddings),
                    len(self._owner_embeddings),
                    len(self._stranger_embeddings)
                    if self._stranger_embeddings is not None
                    else 0,
                )

    def _retrieve(
        self,
        embeds: npt.NDArray[np.float32],
        bank: npt.NDArray[np.float32] | None,
        labels: npt.NDArray[np.str_] | None,
    ) -> tuple[npt.NDArray[np.float32], list[str | None]]:
        scores: npt.NDArray[np.float32] = np.empty(0, dtype=np.float32)
        ids: list[str | None] = []

        if bank is not None and labels is not None:
            sim = embeds @ bank.T
            best = sim.argmax(axis=-1)
            scores = np.array([sim[i, best[i]] for i in range(len(embeds))])
            ids = [str(labels[best[i]]) for i in range(len(embeds))]
        else:
            scores = np.full(embeds.shape[0], _NO_MATCH)
            ids = [None] * embeds.shape[0]

        return scores, ids

    # -- Auto-extend enrollment --------------------------------------------------
    #
    # Users typically upload frontal shots, but a ceiling/desk camera mostly
    # sees them side-on. Those side views miss the frontal bank, get flagged as
    # strangers, and spawn duplicate "stranger_N" identities. To fix this each
    # user gets a second, dynamically-grown "extended" bank: every time a live
    # frame matches them confidently we may keep that frame's embedding as an
    # extra reference view — but only if it is DIFFERENT enough from what we
    # already store (so the bank captures new poses instead of near-duplicates),
    # and capped at ``max_extended_images`` most-diverse samples. The uploaded
    # images are never touched; the extended bank only ever grows recall.

    @staticmethod
    def _user_embeddings(
        bank: npt.NDArray[np.float32] | None,
        labels: npt.NDArray[np.str_] | None,
        raw_label: str,
    ) -> npt.NDArray[np.float32] | None:
        """Rows of ``bank`` whose label equals ``raw_label`` (a friend_* id), or
        None if the bank is empty or holds nothing for that user."""
        if bank is None or labels is None:
            return None
        mask = labels == raw_label
        if not np.any(mask):
            return None
        return bank[mask]

    def _maybe_extend_user(
        self,
        raw_label: str,
        embedding: npt.NDArray[np.float32],
        crop: npt.NDArray[np.uint8] | None,
    ) -> None:
        """Consider folding one confidently-matched live view into a user's
        extended set AND persisting it to disk. Manages its own locking.

        ``embedding`` is assumed L2-normalized (as produced in ``detect``).
        ``crop`` is the BGR face crop to persist; if it is None/empty the view is
        not added, keeping memory and disk in lock-step (every in-memory extended
        embedding has a backing file).

        IMPORTANT: this runs on the ``detect`` hot path, so it NEVER holds
        ``self._lock`` across disk I/O. The lock is taken only for two short,
        pure-memory critical sections (snapshot the existing views; append +
        prune the arrays); the JPEG/sidecar write and any eviction ``unlink``s
        happen with the lock released. Holding the lock across ``cv2.imwrite`` /
        ``np.save`` (as an earlier version did) lengthened every recognized
        frame's lock hold and contended with the photos-watcher reload, which
        widened the ``load_from_disk`` swap window enough to make friends
        momentarily score ``_NO_MATCH``.

        Diversity gate: compute the max cosine similarity between the new view
        and everything already stored for this user (uploads + extended). If it
        exceeds ``diversity_threshold`` (default 0.7) the view is a near-
        duplicate of one we already have -> skip. Otherwise it shows a pose the
        set lacks (e.g. a ~0.35 side-view that only just cleared the confidence
        threshold) -> persist it, append it, and prune back to the most diverse
        samples.
        """
        if crop is None or crop.size == 0:
            return

        # (1) Short lock: snapshot this user's existing views (cheap, in-memory).
        with self._lock:
            enroll = self._user_embeddings(
                self._owner_embeddings, self._owner_labels, raw_label
            )
            extended = self._user_embeddings(
                self._extended_embeddings, self._extended_labels, raw_label
            )
            existing = [e for e in (enroll, extended) if e is not None and len(e)]
            existing_stack = np.concatenate(existing) if existing else None

        # Gate 1 — cheap near-duplicate reject (no lock). A slightly stale
        # snapshot is harmless: the worst case is storing one near-duplicate,
        # which the next prune trims. This is a fast pre-filter (one matmul); the
        # more expensive diversity selection below only runs if it passes.
        max_sim: float | None = None
        if existing_stack is not None:
            max_sim = float(np.max(existing_stack @ embedding))
            if max_sim > self._diversity_threshold:
                # Redundant — we already store an almost identical view.
                logger.debug(
                    "[face] extended '%s': skip redundant view "
                    "(max_sim=%.3f > %.2f)",
                    raw_label.removeprefix(self.FRIEND_PREFIX),
                    max_sim,
                    self._diversity_threshold,
                )
                return

        # Gate 2 — decide keep/drop IN MEMORY, before touching disk. Admission
        # only competes once the set is already full: run the SAME farthest-point
        # selection the prune step uses and check whether the new view would
        # survive it. If it would merely be written and then pruned away on the
        # same frame, skip the disk write entirely. This kills the write-then-
        # immediately-delete churn on the detect hot path — clearing gate 1 is
        # far more permissive than winning a top-``max_extended_images`` slot, so
        # a newly captured view is frequently the least-diverse of the full set.
        # A stale snapshot stays harmless: the authoritative prune re-runs under
        # the lock at commit and remains correct regardless.
        n_existing_ext = 0 if extended is None else len(extended)
        if n_existing_ext + 1 > self._max_extended_images:
            candidates = (
                np.concatenate([extended, embedding[None, :]])
                if extended is not None
                else embedding[None, :]
            )
            keep_local = self._select_diverse(
                candidates, enroll, self._max_extended_images
            )
            if (len(candidates) - 1) not in keep_local:
                logger.debug(
                    "[face] extended '%s': skip view — not among the %d most "
                    "diverse (max_sim=%s)",
                    raw_label.removeprefix(self.FRIEND_PREFIX),
                    self._max_extended_images,
                    "n/a" if max_sim is None else f"{max_sim:.3f}",
                )
                return

        # (2) Persist WITHOUT the lock held. A view enters the in-memory bank
        # only once it has a backing file, so the two never drift apart.
        path = self._save_extended_view(raw_label, embedding, crop)
        if path is None:
            return

        # (3) Short lock: append + prune (pure array ops). Evicted files are
        # collected here and deleted AFTER the lock is released.
        with self._lock:
            self._extended_embeddings = (
                np.concatenate([self._extended_embeddings, embedding[None, :]])
                if self._extended_embeddings is not None
                else embedding[None, :].copy()
            )
            self._extended_labels = (
                np.concatenate([self._extended_labels, np.array([raw_label])])
                if self._extended_labels is not None
                else np.array([raw_label])
            )
            self._extended_paths = (
                np.concatenate([self._extended_paths, np.array([path], dtype=object)])
                if self._extended_paths is not None
                else np.array([path], dtype=object)
            )
            dropped = self._prune_extended_set(raw_label)
            kept = self._user_embeddings(
                self._extended_embeddings, self._extended_labels, raw_label
            )
            n_kept = 0 if kept is None else len(kept)

        for dropped_path in dropped:
            self._delete_extended_view(dropped_path)

        # Only report an ADD when the new view actually stayed. Gate 2 already
        # skips the common "would be pruned immediately" case before writing, but
        # a concurrent add can still change the set between that in-memory
        # decision and the locked prune; if the authoritative prune then evicted
        # THIS view its file is in ``dropped`` (just deleted), so don't log it as
        # added.
        if path in dropped:
            logger.debug(
                "[face] extended '%s': view pruned on commit (race) -> %s",
                raw_label.removeprefix(self.FRIEND_PREFIX),
                path,
            )
            return
        logger.info(
            "[face] extended '%s': ADDED view (%d/%d kept, "
            "max_sim_to_existing=%s) -> %s",
            raw_label.removeprefix(self.FRIEND_PREFIX),
            n_kept,
            self._max_extended_images,
            "n/a" if max_sim is None else f"{max_sim:.3f}",
            path,
        )

    @staticmethod
    def _select_diverse(
        candidates: npt.NDArray[np.float32],
        anchor: npt.NDArray[np.float32] | None,
        k: int,
    ) -> list[int]:
        """Greedy farthest-point selection: return up to ``k`` indices of
        ``candidates`` (each row an embedding) that are most diverse.

        Starting from ``anchor`` (the user's fixed uploads) as reference points,
        repeatedly keep the candidate whose similarity to everything already
        kept (anchor + kept candidates) is LOWEST — the farthest / most novel
        pose. This packs the slots with views that best complement the frontal
        uploads (side-views, tilts) rather than more frontals. If ``anchor`` is
        None/empty, the newest candidate (last row) seeds the selection.
        """
        m = len(candidates)
        if m <= k:
            return list(range(m))

        if anchor is not None and len(anchor):
            selected_ref: list[npt.NDArray[np.float32]] = [anchor]
            selected_local: list[int] = []
        else:
            seed = m - 1  # newest view
            selected_ref = [candidates[seed][None, :]]
            selected_local = [seed]

        remaining = [j for j in range(m) if j not in selected_local]
        while len(selected_local) < k and remaining:
            ref = np.concatenate(selected_ref)  # (K, D)
            sims = candidates[remaining] @ ref.T  # (R, K)
            # Closeness of each candidate to its nearest already-kept view;
            # the smallest such value is the most novel candidate.
            nearest = sims.max(axis=1)
            pick = int(np.argmin(nearest))
            chosen = remaining.pop(pick)
            selected_local.append(chosen)
            selected_ref.append(candidates[chosen][None, :])
        return selected_local

    def _prune_extended_set(self, raw_label: str) -> list[str]:
        """Trim one user's extended bank to the ``max_extended_images`` most
        diverse views. Caller must hold ``self._lock``.

        Only touches the in-memory arrays; it RETURNS the on-disk paths of the
        evicted views so the caller can delete their files AFTER releasing the
        lock (disk I/O must never run under ``self._lock`` — see
        ``_maybe_extend_user``). Returns an empty list when nothing is evicted.
        """
        if (
            self._extended_embeddings is None
            or self._extended_labels is None
            or self._extended_paths is None
        ):
            return []

        mask = self._extended_labels == raw_label
        idxs = np.nonzero(mask)[0]
        if len(idxs) <= self._max_extended_images:
            return []

        candidates = self._extended_embeddings[idxs]  # (M, D), newest is last
        anchor = self._user_embeddings(
            self._owner_embeddings, self._owner_labels, raw_label
        )
        keep_local = self._select_diverse(candidates, anchor, self._max_extended_images)
        keep_global = idxs[np.array(sorted(keep_local))]

        dropped = [
            str(self._extended_paths[gi]) for gi in np.setdiff1d(idxs, keep_global)
        ]

        keep_mask = ~mask  # keep every OTHER user's rows untouched
        keep_mask[keep_global] = True
        self._extended_embeddings = self._extended_embeddings[keep_mask]
        self._extended_labels = self._extended_labels[keep_mask]
        self._extended_paths = self._extended_paths[keep_mask]
        return dropped

    # -- Extended-set persistence (disk) -----------------------------------------

    def _extended_dir_for(self, raw_label: str) -> Path:
        """Per-user directory holding auto-captured extended views.

        The user's on-disk folder name is exactly the friend label without the
        FRIEND_PREFIX (load_from_disk labels each user by their folder name), so
        no re-normalization is needed here.
        """
        folder = raw_label.removeprefix(self.FRIEND_PREFIX)
        return USERS_DIR / folder / _EXTENDED_SUBDIR

    def _save_extended_view(
        self,
        raw_label: str,
        embedding: npt.NDArray[np.float32],
        crop: npt.NDArray[np.uint8],
    ) -> str | None:
        """Persist one extended view: a JPEG crop + a sidecar .npy embedding.

        Returns the JPEG path on success, or None if it could not be written (in
        which case the caller must NOT add the view to the in-memory bank). The
        sidecar embedding is what a later load trusts, so a restart reloads the
        exact vector and never has to re-detect the (possibly hard) pose.
        """
        try:
            dest = self._extended_dir_for(raw_label)
            dest.mkdir(parents=True, exist_ok=True)
            # Millisecond stamp keeps names sortable; the seq suffix guarantees
            # uniqueness even for two captures within the same millisecond. The
            # counter bump is the only locked step here — the file writes below
            # run WITHOUT the lock (this method is called off the lock).
            with self._lock:
                self._extended_save_seq += 1
                seq = self._extended_save_seq
            stem = f"ext_{int(time.time() * 1000)}_{seq}"
            img_path = dest / f"{stem}{_EXTENDED_IMG_EXT}"
            emb_path = dest / f"{stem}{_EXTENDED_EMB_EXT}"
            if not cv2.imwrite(str(img_path), crop):
                logger.warning("[face-v2] cv2.imwrite failed for %s", img_path)
                return None
            np.save(emb_path, embedding.astype(np.float32))
            return str(img_path)
        except (OSError, cv2.error) as e:
            logger.warning("[face-v2] failed to save extended view: %s", e)
            return None

    @staticmethod
    def _delete_extended_view(img_path: str) -> None:
        """Delete an extended view's JPEG and its sidecar .npy (best-effort)."""
        try:
            p = Path(img_path)
            p.unlink(missing_ok=True)
            p.with_suffix(_EXTENDED_EMB_EXT).unlink(missing_ok=True)
        except OSError as e:
            logger.warning(
                "[face-v2] failed to delete extended view %s: %s", img_path, e
            )

    def _load_extended_embedding(
        self, img_path: Path, expected_dim: int | None = None
    ) -> npt.NDArray[np.float32] | None:
        """Return the L2-normalized embedding for one persisted extended view.

        Fast path: the sidecar .npy next to the JPEG (exact, no inference). It is
        trusted as-is because it was validated when captured — crucially we do
        NOT re-gate it against the uploads, since a legitimate side-view may only
        match other extended views, not the frontal uploads. If ``expected_dim``
        is given and the sidecar's length differs (a model swap invalidated it),
        the sidecar is ignored and we re-embed the JPEG with the current model.
        Fallback: re-embed the JPEG and take the largest detected face, then
        rewrite the sidecar. Returns None if neither yields an embedding.
        """
        emb_path = img_path.with_suffix(_EXTENDED_EMB_EXT)
        if emb_path.is_file():
            try:
                emb = np.load(emb_path).astype(np.float32).reshape(-1)
                n = float(np.linalg.norm(emb))
                if n > 0 and (expected_dim is None or emb.shape[0] == expected_dim):
                    return emb / n
            except (OSError, ValueError) as e:
                logger.warning("[face-v2] bad extended sidecar %s: %s", emb_path, e)

        if self._app is None:
            return None
        img = cv2.imread(str(img_path))
        if img is None:
            return None
        results = self._app.get(img)
        if not results:
            return None
        best = max(
            results,
            key=lambda r: max(r["bbox"][2] - r["bbox"][0], 0)
            * max(r["bbox"][3] - r["bbox"][1], 0),
        )
        emb = best["embedding"].astype(np.float32)
        n = float(np.linalg.norm(emb))
        if n == 0:
            return None
        emb = emb / n
        try:
            np.save(emb_path, emb)
        except OSError:
            pass
        return emb

    def _read_extended_for(
        self,
        person_name: str,
        expected_dim: int | None,
        anchor: npt.NDArray[np.float32] | None,
    ) -> tuple[list[npt.NDArray[np.float32]], list[str]]:
        """Read one user's persisted extended views from disk. PURE reader: no
        lock, no in-memory mutation — it only touches the filesystem and returns
        ``(embeddings, paths)`` for the caller to install atomically.

        Each view's sidecar embedding is trusted as-is (it was validated at
        capture); we deliberately do NOT re-gate against the uploads, since a
        genuine side-view often matches only other extended views. A view is
        dropped (and its files removed) only when it is truly unusable — no
        usable sidecar AND no detectable face in the crop. When more than
        ``max_extended_images`` survive (e.g. after a shrunk config), the most
        diverse subset is kept (anchored on ``anchor``, the user's uploads) and
        the rest deleted.
        """
        raw_label = self.FRIEND_PREFIX + person_name
        dest = self._extended_dir_for(raw_label)
        if not dest.is_dir():
            return [], []

        embeds: list[npt.NDArray[np.float32]] = []
        paths: list[str] = []
        for img_path in sorted(dest.glob(f"*{_EXTENDED_IMG_EXT}")):
            emb = self._load_extended_embedding(img_path, expected_dim=expected_dim)
            if emb is None:
                # Neither a usable sidecar nor a detectable face — drop it.
                self._delete_extended_view(str(img_path))
                continue
            embeds.append(emb)
            paths.append(str(img_path))

        if len(embeds) > self._max_extended_images:
            keep = set(
                self._select_diverse(
                    np.stack(embeds), anchor, self._max_extended_images
                )
            )
            for i in range(len(embeds)):
                if i not in keep:
                    self._delete_extended_view(paths[i])
            embeds = [embeds[i] for i in sorted(keep)]
            paths = [paths[i] for i in sorted(keep)]
        return embeds, paths

    def reload(
        self,
        owner_images: list[cv2.typing.MatLike],
        owner_labels: list[str],
        person_names: list[str],
    ) -> None:
        """Atomically rebuild the owner AND extended banks from disk.

        Fixes the reload race: the previous flow cleared the owner bank and then
        re-appended per person, leaving a window in which ``detect`` saw a
        None/partial owner bank and scored every friend ``_NO_MATCH``. Here ALL
        heavy work — owner embedding inference and extended disk reads — happens
        WITHOUT the lock, and a single locked swap installs both banks at once,
        so ``detect`` only ever sees the complete old set or the complete new
        set, never an intermediate.

        ``owner_labels`` are the raw (folder-name) labels for ``owner_images``;
        ``person_names`` are all enrolled folder names whose ``.extended`` sets
        should be restored (a superset of the labels is fine — empty ones no-op).
        """
        if self._app is None:
            msg = f"[{self.__class__.__name__}] service must be started first"
            raise RuntimeError(msg)

        # 1. Owner embeddings (inference, no lock).
        prefixed = [self.FRIEND_PREFIX + str(lbl) for lbl in owner_labels]
        o_embeds: list[npt.NDArray[np.float32]] = []
        o_labels: list[str] = []
        for image, label in zip(owner_images, prefixed):
            for r in self._app.get(image):
                emb = r["embedding"]
                o_embeds.append(emb / np.linalg.norm(emb))
                o_labels.append(label)
        new_owner_e = np.stack(o_embeds, axis=0) if o_embeds else None
        new_owner_l = np.array(o_labels) if o_labels else None
        expected_dim = int(new_owner_e.shape[1]) if new_owner_e is not None else None

        # 2. Extended views (disk reads, no lock). Anchor each user's diversity
        # on their FRESHLY-computed uploads, decoupled from live state.
        x_embeds: list[npt.NDArray[np.float32]] = []
        x_labels: list[str] = []
        x_paths: list[str] = []
        for name in person_names:
            raw = self.FRIEND_PREFIX + name
            anchor = (
                new_owner_e[new_owner_l == raw] if new_owner_e is not None else None
            )
            es, ps = self._read_extended_for(name, expected_dim, anchor)
            for e, p in zip(es, ps):
                x_embeds.append(e)
                x_labels.append(raw)
                x_paths.append(p)
        new_ext_e = np.stack(x_embeds, axis=0) if x_embeds else None
        new_ext_l = np.array(x_labels) if x_labels else None
        new_ext_p = np.array(x_paths, dtype=object) if x_paths else None

        # 3. Single atomic swap of both banks.
        with self._lock:
            self._owner_embeddings = new_owner_e
            self._owner_labels = new_owner_l
            self._extended_embeddings = new_ext_e
            self._extended_labels = new_ext_l
            self._extended_paths = new_ext_p
        logger.info(
            "Reloaded banks — %d owner view(s), %d extended view(s)",
            0 if new_owner_e is None else len(new_owner_e),
            0 if new_ext_e is None else len(new_ext_e),
        )

    @staticmethod
    def _crop_face(
        frame: npt.NDArray[np.uint8],
        bbox: tuple[int, int, int, int],
        margin: float = 0.3,
    ) -> npt.NDArray[np.uint8] | None:
        """BGR crop around a detection bbox with a relative margin, clamped to
        the frame. The margin gives the reloader enough context to re-detect the
        face if a sidecar embedding is ever missing. None if degenerate.
        """
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = bbox
        bw, bh = x2 - x1, y2 - y1
        if bw <= 0 or bh <= 0:
            return None
        mx, my = int(bw * margin), int(bh * margin)
        x1 = max(0, x1 - mx)
        y1 = max(0, y1 - my)
        x2 = min(w, x2 + mx)
        y2 = min(h, y2 + my)
        if x2 <= x1 or y2 <= y1:
            return None
        return frame[y1:y2, x1:x2].copy()

    def detect(self, frame: cv2.typing.MatLike):
        if self._app is None:
            msg = f"[{self.__class__.__name__}] service must be started first"
            raise RuntimeError(msg)

        H, W = frame.shape[:2]
        frame_area = H * W

        raw_results = self._app.get(frame)
        n_faces = len(raw_results)

        if n_faces == 0:
            return

        embeds: npt.NDArray[np.float32] = np.stack(
            [r["embedding"] / np.linalg.norm(r["embedding"]) for r in raw_results]
        )
        det_scores: npt.NDArray[np.float32] = np.stack(
            [r["det_score"] for r in raw_results]
        )

        with self._lock:
            self._load_strangers_state()

            # Retrieve against the uploads and the extended views SEPARATELY.
            # The owner decision uses the max of the two (identical to matching a
            # single combined bank), but keeping them apart lets the debug log
            # show WHICH set made each match — in particular when the extended
            # set rescued a friend the frontal uploads alone would have missed.
            upload_scores, upload_ids = self._retrieve(
                embeds, self._owner_embeddings, self._owner_labels
            )
            ext_scores, ext_ids = self._retrieve(
                embeds, self._extended_embeddings, self._extended_labels
            )
            stranger_scores, stranger_ids = self._retrieve(
                embeds, self._stranger_embeddings, self._stranger_labels
            )

        # Combined owner score/id per face = whichever bank matched best, plus a
        # tag of WHERE the winning match came from ("enroll" | "extended" | None).
        owner_scores = np.maximum(upload_scores, ext_scores)
        owner_ids: list[str | None] = []
        owner_source: list[str | None] = []
        for _i in range(n_faces):
            ext_won = ext_scores[_i] > upload_scores[_i]
            owner_ids.append(ext_ids[_i] if ext_won else upload_ids[_i])
            if float(owner_scores[_i]) <= _NO_MATCH:
                owner_source.append(None)  # neither bank held anything to match
            else:
                owner_source.append("extended" if ext_won else "enroll")

        new_stranger_embeds = []
        new_stranger_labels = []
        # (friend_raw_label, normalized_embedding, bbox) for confidently-matched
        # faces that may extend their user's set (diversity-gated + cropped +
        # persisted after the loop).
        extend_candidates: list[
            tuple[str, npt.NDArray[np.float32], tuple[int, int, int, int]]
        ] = []
        # per-face: (bbox_pixels, face_kind, label)  face_kind: "friend"|"stranger"|"unsure"
        faces: list[Face] = []

        for i in range(n_faces):
            o_score = float(owner_scores[i])
            s_score = float(stranger_scores[i])
            bbox = [int(v) for v in raw_results[i]["bbox"]]
            x1, y1, x2, y2 = bbox
            face_area = max(x2 - x1, 0) * max(y2 - y1, 0)

            if face_area / frame_area < self._area_ratio_threshold:
                continue

            det_score = det_scores[i]

            if o_score > self._threshold:
                raw_id = owner_ids[i] or ""
                person_id = raw_id.removeprefix(self.FRIEND_PREFIX)
                face_kind = PersonKind.FRIEND
                # Observability: log whether the uploads or the extended set
                # carried this match. The high-signal case is an EXTENDED rescue
                # — the frontal uploads scored at/below threshold but a stored
                # side/angled view pushed it over — which is exactly the benefit
                # this feature exists to deliver.
                up_s = float(upload_scores[i])
                ex_s = float(ext_scores[i])
                if up_s <= self._threshold < ex_s:
                    logger.info(
                        "[face] '%s' RESCUED by extended set "
                        "(enroll_sim=%.3f <= thr=%.2f < extended_sim=%.3f)",
                        person_id, up_s, self._threshold, ex_s,
                    )
                else:
                    logger.debug(
                        "[face] '%s' matched via %s "
                        "(enroll_sim=%.3f, extended_sim=%.3f, thr=%.2f)",
                        person_id, owner_source[i], up_s, ex_s, self._threshold,
                    )
                # Confidently identified: this live view is a candidate to
                # enrich the user's extended set (kept only if it adds a pose
                # the current set lacks — see _maybe_extend_user).
                if raw_id:
                    extend_candidates.append((raw_id, embeds[i], (x1, y1, x2, y2)))
            elif s_score > self._threshold:
                raw_id = stranger_ids[i] or ""
                person_id = raw_id.removeprefix(self.STRANGER_PREFIX)
                face_kind = PersonKind.STRANGER
            elif (
                self._negative_threshold is None
                or max(o_score, s_score) <= self._negative_threshold
            ):
                with self._lock:
                    self._stranger_counter += 1
                    self._stranger_counter %= int(1e6)

                    raw_id = f"{self.STRANGER_PREFIX}stranger_{self._stranger_counter}"
                person_id = raw_id.removeprefix(self.STRANGER_PREFIX)
                face_kind = PersonKind.STRANGER

                new_stranger_embeds.append(embeds[i])
                new_stranger_labels.append(raw_id)
            else:
                # Score between negative_threshold and threshold on both banks — unsure
                person_id = "?"
                face_kind = PersonKind.UNSURE

            faces.append(
                Face(
                    bbox=bbox, kind=face_kind, person_id=person_id, confidence=det_score
                )
            )

        if new_stranger_embeds:
            stacked_e = np.stack(new_stranger_embeds, axis=0)
            stacked_l = np.stack(new_stranger_labels, axis=0)
            with self._lock:
                self._stranger_embeddings = (
                    np.concatenate([self._stranger_embeddings, stacked_e])
                    if self._stranger_embeddings is not None
                    else stacked_e
                )
                self._stranger_labels = (
                    np.concatenate([self._stranger_labels, stacked_l])
                    if self._stranger_labels is not None
                    else stacked_l
                )
                self._evict_oldest_strangers()
                self._save_strangers_state()

        # Auto-extend enrollment: crop each confidently-matched view and fold it
        # into its user's extended set. _maybe_extend_user manages its own
        # locking and keeps disk I/O OFF the lock, so we deliberately do NOT wrap
        # this in `with self._lock` (that previously held the lock across every
        # frame's JPEG write and widened the reload race).
        if extend_candidates:
            for raw_label, emb, bbox in extend_candidates:
                crop = self._crop_face(frame, bbox)
                self._maybe_extend_user(raw_label, emb, crop)

        return faces

    def _evict_oldest_strangers(self) -> None:
        if self._stranger_embeddings is None or self._stranger_labels is None:
            return

        count = len(self._stranger_embeddings)
        if count <= self._max_strangers:
            return
        drop = count - self._max_strangers
        logger.debug("Evicting %d oldest stranger(s)", drop)
        self._stranger_embeddings = self._stranger_embeddings[drop:]
        self._stranger_labels = self._stranger_labels[drop:]

    def _save_strangers_state(self):
        if self._stranger_embeddings is not None and self._stranger_labels is not None:
            try:
                np.save(STRANGER_STATE_DIR / "embeds.npy", self._stranger_embeddings)
                np.save(STRANGER_STATE_DIR / "labels.npy", self._stranger_labels)
                np.save(
                    STRANGER_STATE_DIR / "counter.npy", np.array(self._stranger_counter)
                )
                logger.debug("Saved strangers' state")
            except Exception as e:
                logger.error(f"Failed to save strangers' state due to {e}")

    def _load_strangers_state(self):
        try:
            stranger_embeddings = np.load(
                STRANGER_STATE_DIR / "embeds.npy", allow_pickle=True
            )
            stranger_labels = np.load(
                STRANGER_STATE_DIR / "labels.npy", allow_pickle=True
            )
            stranger_counter = int(
                np.load(STRANGER_STATE_DIR / "counter.npy", allow_pickle=True)
            )
        except Exception:
            logger.exception("Failed to load strangers' state")
            stranger_embeddings = None
            stranger_labels = None
            stranger_counter = 0

        if stranger_embeddings is not None and stranger_labels is not None:
            self._stranger_embeddings = stranger_embeddings
            self._stranger_labels = stranger_labels
            self._stranger_counter = stranger_counter


class FacePerception(Perception[cv2.typing.MatLike]):
    """SCRFD + ONNX-landmark + EdgeFace face recognizer. Detects friends and strangers, fires presence events."""

    FRIEND_PREFIX: str = "friend_"
    STRANGER_PREFIX: str = "stranger_"

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        presense_service: PresenseService | None = None,
        threshold: float = 0.3,
        negative_threshold: float | None = 0.2,
        max_strangers: int = 50,
        area_ratio_threshold: float = config.FACE_AREA_RATIO_THRESHOLD,
        owners_forget_ts: float = config.FACE_OWNER_FORGET_S,
        strangers_forget_ts: float = config.FACE_STRANGER_FORGET_S,
        max_extended_images: int = 5,
        diversity_threshold: float = 0.8,
    ):
        super().__init__(perception_state, send_event)

        self._presense_service: PresenseService | None = presense_service
        self._face_recognizer: FaceRecognizer = FaceRecognizer(
            area_ratio_threshold=area_ratio_threshold,
            threshold=threshold,
            negative_threshold=negative_threshold,
            max_strangers=max_strangers,
            max_extended_images=max_extended_images,
            diversity_threshold=diversity_threshold,
        )
        self._face_recognizer.start()
        self._owners_forget_ts: float = owners_forget_ts
        self._strangers_forget_ts: float = strangers_forget_ts

        self._faces_n: int = 0
        self._face_present: bool = False
        self._people_data_dict: dict[str, PersonData] = {}
        self._owners: set[str] = set()
        self._strangers: set[str] = set()

        self._any_stranger_logged: bool = False
        self._stranger_visit_counts: dict[str, Any] = self._load_stranger_stats()

        # Stranger snapshot buffer — flushed every FACE_STRANGER_FLUSH_S
        # Each entry: (raw_frame, annotations[(bbox, kind, label), ...])
        self._stranger_flush_interval: float = config.FACE_STRANGER_FLUSH_S
        self._stranger_snapshots_buffers: list[cv2.typing.MatLike] = []
        self._stranger_ids_buffer: set[str] = set()
        self._last_stranger_flush_ts: float = 0.0

        self._callbacks: set[Callable[[FaceDetectionData], None]] = set()

        self._state_lock: threading.RLock = threading.RLock()
        self._callback_lock: threading.RLock = threading.RLock()

        self._start_watcher()

    def register_callback(self, callback: Callable[[FaceDetectionData], None]):
        with self._callback_lock:
            self._callbacks.add(callback)

    def unregister_callback(self, callback: Callable[[FaceDetectionData], None]):
        with self._callback_lock:
            self._callbacks.discard(callback)

    def _start_watcher(self) -> None:
        """Poll USERS_DIR every 2s and reload embeddings when files change."""
        USERS_DIR.mkdir(parents=True, exist_ok=True)

        def _latest_mtime() -> float:
            # Only watch what ``load_from_disk`` actually consumes: image files
            # sitting DIRECTLY in a user folder (USERS_DIR/<user>/<file>). Match
            # that exact set so a reload fires iff an enrollment upload changed.
            #
            # This deliberately ignores everything else under USERS_DIR:
            #   * subfolders — ``.extended`` (self-managed, already in the bank),
            #     ``mood/``, ``pose/``, and any other per-user data dir. They are
            #     never loaded as enrollment images, so a change there must not
            #     trip a full re-embed of every upload. The depth check below
            #     (parent is a direct child of USERS_DIR) excludes all of them,
            #     including subfolders added in the future — no blocklist to keep.
            #   * directory entries — skipped via ``is_file``; e.g. creating a
            #     user's ``.extended`` subfolder bumps the parent user folder's
            #     mtime, which would otherwise look like an upload change.
            #   * non-image files — ``metadata.json``, ``.stranger_stats.json``,
            #     etc., excluded by the suffix check.
            try:
                return max(
                    (
                        e.stat().st_mtime
                        for e in USERS_DIR.rglob("*")
                        if e.is_file()
                        and e.suffix.lower() in _ENROLL_IMG_EXTS
                        and e.parent.parent == USERS_DIR
                    ),
                    default=0.0,
                )
            except OSError:
                return 0.0

        def _poll():
            last = _latest_mtime()
            while True:
                time.sleep(2)
                current = _latest_mtime()
                if current != last:
                    last = current
                    logger.info("User photos changed — reloading embeddings")
                    _ = self.load_from_disk()

        t = threading.Thread(target=_poll, daemon=True, name="owner-photos-watcher")
        t.start()
        logger.info("Watching users dir: %s", USERS_DIR)

    def train(
        self,
        images: list[cv2.typing.MatLike],
        labels: list[str],
    ) -> None:
        self._face_recognizer.register(images, labels)

    @staticmethod
    def normalize_label(label: str) -> str:
        """Lowercase folder-safe label (a-z0-9_-)."""
        s = label.strip().lower()
        s = re.sub(r"[^a-z0-9_-]+", "_", s)
        s = s.strip("_")
        return s[:64] if s else "person"

    def _clear_owner_embeddings(self) -> None:
        self._face_recognizer.reset(owners=True, strangers=False)

    @staticmethod
    def _read_metadata(person_dir: Path) -> dict[str, Any]:
        """Read metadata.json from a person's folder. Returns {} if missing."""
        meta_path = person_dir / "metadata.json"
        if meta_path.is_file():
            try:
                return json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    @staticmethod
    def _write_metadata(
        person_dir: Path, telegram_username: str = "", telegram_id: str = ""
    ) -> None:
        """Write metadata.json with telegram info."""
        meta_path = person_dir / "metadata.json"
        data: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                data = json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        if telegram_username:
            data["telegram_username"] = telegram_username
        if telegram_id:
            data["telegram_id"] = telegram_id
        _ = meta_path.write_text(json.dumps(data))

    def save_photo(
        self,
        image_bytes: bytes,
        label: str,
        telegram_username: str = "",
        telegram_id: str = "",
    ) -> str:
        """Write JPEG bytes under USERS_DIR/{label}/ with a timestamp name."""
        norm = self.normalize_label(label)
        dest_dir = USERS_DIR / norm
        dest_dir.mkdir(parents=True, exist_ok=True)
        if telegram_username or telegram_id:
            self._write_metadata(dest_dir, telegram_username, telegram_id)
        fname = f"{int(time.time() * 1000)}.jpg"
        path = dest_dir / fname
        _ = path.write_bytes(image_bytes)
        return str(path)

    def load_from_disk(self) -> int:
        """Re-train the owner + extended banks from all images under USERS_DIR.

        Gathers every user's uploads (and folder names) first, then hands them to
        ``FaceRecognizer.reload`` which installs the freshly-built owner and
        extended banks in a SINGLE atomic swap. This is deliberate: an earlier
        version cleared the bank and re-appended per person, leaving a window in
        which a concurrent ``detect`` saw a None/partial owner bank and scored
        every enrolled friend ``_NO_MATCH`` (they'd flip to stranger/unsure for a
        frame). One atomic swap closes that window.
        """
        if not USERS_DIR.is_dir():
            logger.info("No users dir at %s — skipping", USERS_DIR)
            # Empty inputs => reload clears both banks atomically.
            self._face_recognizer.reload([], [], [])
            return 0

        _IMG_EXTS = _ENROLL_IMG_EXTS
        all_images: list[cv2.typing.MatLike] = []
        all_labels: list[str] = []
        person_names: list[str] = []
        loaded_total = 0

        for person_dir in sorted(USERS_DIR.iterdir()):
            if not person_dir.is_dir():
                continue
            person_names.append(person_dir.name)

            count = 0
            for fname in sorted(person_dir.iterdir()):
                if fname.suffix.lower() not in _IMG_EXTS:
                    continue
                img = cv2.imread(str(fname))
                if img is None:
                    logger.warning("Failed to load image: %s", fname)
                    continue
                all_images.append(img)
                all_labels.append(person_dir.name)
                count += 1

            if count:
                loaded_total += count
                logger.info("Loaded %d image(s) for '%s'", count, person_dir.name)

        # Atomic rebuild of both banks (owner inference + extended disk reads all
        # happen off the lock inside reload; only the final swap is locked).
        self._face_recognizer.reload(all_images, all_labels, person_names)

        n_owners = len(self._face_recognizer.owners)
        n_strangers = len(self._face_recognizer.strangers)
        logger.info(
            "Load from disk done — %d image(s), %d enrolled owners(s), %d enrolled strangers(s)",
            loaded_total,
            n_owners,
            n_strangers,
        )
        return n_owners

    def enroll_from_bytes(
        self,
        image_bytes: bytes,
        label: str,
        telegram_username: str = "",
        telegram_id: str = "",
    ) -> str:
        """Decode image, save as JPEG on disk, and append embeddings."""
        norm = self.normalize_label(label)
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("could not decode image")
        ok, buf = cv2.imencode(".jpg", img)
        if not ok:
            raise ValueError("could not encode image")
        path = self.save_photo(buf.tobytes(), norm, telegram_username, telegram_id)
        self.train([img], [norm])
        return path

    @staticmethod
    def _resolve_person_dir(label: str) -> Path | None:
        """Find the actual person directory on disk, handling case mismatches."""
        norm = FacePerception.normalize_label(label)
        direct = USERS_DIR / norm
        if direct.is_dir():
            return direct
        if not USERS_DIR.is_dir():
            return None
        for child in USERS_DIR.iterdir():
            if child.is_dir() and child.name.lower() == norm:
                return child
        return None

    def get_telegram_id(self, label: str) -> str | None:
        """Return telegram_id for a person, or None if not set."""
        person_dir = self._resolve_person_dir(label)
        if person_dir is None:
            return None
        meta = self._read_metadata(person_dir)
        return meta.get("telegram_id") or None

    def remove_photo(self, label: str, filename: str) -> bool:
        """Remove a single photo from a person's directory and re-load from disk.
        Returns True if the photo was found and deleted."""
        person_dir = self._resolve_person_dir(label)
        if person_dir is None:
            return False
        photo_path = person_dir / filename
        if not photo_path.is_file():
            return False
        photo_path.unlink()
        logger.info("Removed photo %s for '%s'", filename, label)
        _IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
        remaining = [f for f in person_dir.iterdir() if f.suffix.lower() in _IMG_EXTS]
        if not remaining:
            shutil.rmtree(person_dir)
            logger.info("No photos left for '%s' — removed person directory", label)
        _ = self.load_from_disk()
        return True

    def remove_person(self, label: str) -> bool:
        """Remove one person's directory and re-load remaining persons from disk."""
        person_dir = self._resolve_person_dir(label)
        if person_dir is None:
            return False
        shutil.rmtree(person_dir)
        _ = self.load_from_disk()
        return True

    def enrolled_count(self) -> int:
        return len(self._face_recognizer.owners)

    def enrolled_names(self) -> list[str]:
        return self._face_recognizer.owners

    def reset_enrolled(self) -> None:
        """Clear enrolled embeddings and delete all saved photos. Stranger bank is unchanged."""
        self._clear_owner_embeddings()
        if USERS_DIR.is_dir():
            for child in USERS_DIR.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
        logger.info("Enrolled embeddings cleared and photos removed")

    @override
    def cleanup(self) -> None:
        pass

    @override
    def _check_impl(self, data: cv2.typing.MatLike) -> None:
        frame = data
        if frame is None:
            logger.debug("[face] frame is None, skipping")
            return

        cur_ts = time.time()

        faces = self._face_recognizer.detect(frame)

        with self._state_lock:
            if not faces:
                logger.debug("[face] no faces detected")
                self._face_present = False
                self._faces_n = 0
                self._check_leaves(cur_ts)
                return
            else:
                logger.debug("[face] detected %d face(s): %s", len(faces), [f.person_id for f in faces])
                self._faces_n = len(faces)
                self._face_present = len(faces) > 0

            owners_seen = set(
                [f.person_id for f in faces if f.kind == PersonKind.FRIEND]
            )
            strangers_seen = set(
                [f.person_id for f in faces if f.kind == PersonKind.STRANGER]
            )

            logger.info(
                f"Detected friends={list(owners_seen)} and strangers={list(strangers_seen)}"
            )

            new_owners: set[str] = set()
            new_strangers: set[str] = set()

            for f in faces:
                if f.kind == PersonKind.UNSURE:
                    continue

                person_id = f.person_id
                if person_id not in self._people_data_dict:
                    self._people_data_dict[person_id] = PersonData(
                        id=person_id, kind=f.kind
                    )

                face_data = self._people_data_dict[person_id]

                if face_data.kind == PersonKind.FRIEND:
                    forget_ts = self._owners_forget_ts
                elif face_data.kind == PersonKind.STRANGER:
                    forget_ts = self._strangers_forget_ts
                else:
                    forget_ts = 0

                if (
                    face_data.last_seen is None
                    or (cur_ts - face_data.last_seen) > forget_ts
                ):
                    if face_data.kind == PersonKind.FRIEND:
                        new_owners.add(person_id)
                        self._post_wellbeing(
                            self.normalize_label(person_id), "enter"
                        )
                    elif face_data.kind == PersonKind.STRANGER:
                        new_strangers.add(person_id)

                    self._people_data_dict[person_id].last_session_time = cur_ts

                self._people_data_dict[person_id].last_seen = cur_ts

            if len(new_strangers) > 0 and not self._any_stranger_logged:
                self._post_wellbeing("unknown", "enter")
                self._any_stranger_logged = True

            if self._face_present and self._presense_service is not None:
                self._presense_service.on_motion()

            # Strangers: always buffer snapshots; flush decides when to send
            annotated_frame = self._annotate_frame(frame, faces)
            annotated_frames_to_send: list[cv2.typing.MatLike] = []
            if len(new_owners) > 0:
                annotated_frames_to_send.append(annotated_frame)
            else:
                if new_strangers:
                    self._stranger_snapshots_buffers.append(annotated_frame)
                    self._stranger_ids_buffer.update(new_strangers)

            familiar_paths: dict[str, str] = {}
            if new_strangers:
                just_familiar = self._track_stranger_visits(new_strangers)
                if just_familiar:
                    ts_ms = int(cur_ts * 1000)
                    try:
                        _STRANGER_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
                    except OSError as e:
                        logger.warning(
                            "[face] failed to create familiar snapshots dir: %s", e
                        )
                    for sid in just_familiar:
                        path = _STRANGER_SNAPSHOTS_DIR / f"{sid}_{ts_ms}.jpg"
                        try:
                            if cv2.imwrite(str(path), frame):
                                familiar_paths[sid] = str(path)
                            else:
                                logger.warning(
                                    "[face] cv2.imwrite returned False for %s", path
                                )
                        except cv2.error as e:
                            logger.warning(
                                "[face] failed to save familiar snapshot %s: %s",
                                path,
                                e,
                            )

            flushed_stranger_snapshots, flushed_stranger_ids = (
                self._flush_stranger_buffer(cur_ts)
            )

            annotated_frames_to_send = (
                annotated_frames_to_send + flushed_stranger_snapshots
            )
            stranger_ids_to_send = new_strangers.union(flushed_stranger_ids)

            if annotated_frames_to_send:
                parts = []
                if new_owners:
                    parts.append(f"friend ({', '.join(new_owners)})")
                if stranger_ids_to_send:
                    parts.append(f"stranger ({', '.join(stranger_ids_to_send)})")
                summary = ", ".join(parts)
                total_faces = len(new_owners) + len(stranger_ids_to_send)
                message = f"Person detected — {total_faces} face(s) visible ({summary})"
                for sid, img_path in familiar_paths.items():
                    message += (
                        f" (familiar stranger {sid} — seen "
                        f"{_FAMILIAR_VISIT_THRESHOLD} times, ask user if they "
                        f"want to remember this face; image saved at {img_path})"
                    )
                self._send_enter_event(
                    frames=annotated_frames_to_send,
                    message=message,
                )

            self._check_leaves(cur_ts)

            face_detection_data = FaceDetectionData(
                frame=frame.copy(), faces=copy(faces)
            )

            self._perception_state.detected_faces.data = face_detection_data
            self._perception_state.current_user.data = self.current_user()

        with self._callback_lock:
            for callback in self._callbacks:
                callback(face_detection_data)

    def to_dict(self) -> dict[str, Any]:
        with self._state_lock:
            cur_ts = time.time()
            last_person: str | None = None
            last_seen: float | None = None

            for person_id, person_data in self._people_data_dict.items():
                if person_data.last_seen is None:
                    continue

                if last_seen is None or last_seen < person_data.last_seen:
                    last_seen = person_data.last_seen
                    last_person = person_id
            # Currently visible people
            return {
                "type": "face",
                "face_present": self._face_present,
                "faces_count": self._faces_n,
                "visible": list(self._people_data_dict.keys()),
                "last_person": last_person,
                "last_seen_seconds_ago": (cur_ts - last_seen)
                if last_seen is not None
                else None,
                "enrolled_count": self.enrolled_count(),
                "stranger_count": len(self._face_recognizer.strangers),
            }

    # -- Presence leave detection ------------------------------------------------

    def _check_leaves(self, cur_ts: float) -> None:
        """Fire presence.leave for anyone not seen within their forget interval."""
        deleted_ids: set[str] = set()
        with self._state_lock:
            for person_id, person_data in self._people_data_dict.items():
                if person_data.kind == PersonKind.FRIEND:
                    if (
                        person_data.last_seen is None
                        or (cur_ts - person_data.last_seen) > self._owners_forget_ts
                    ):
                        deleted_ids.add(person_id)
                        # Per-friend leave row on their own timeline.
                        self._post_wellbeing(self.normalize_label(person_id), "leave")
                        self._send_leave_event(person_id, kind=person_data.kind)
                elif person_data.kind == PersonKind.STRANGER:
                    if (
                        person_data.last_seen is None
                        or (cur_ts - person_data.last_seen) > self._strangers_forget_ts
                    ):
                        deleted_ids.add(person_id)

            for id in deleted_ids:
                del self._people_data_dict[id]

            current_strangers = [
                p
                for p in self._people_data_dict.values()
                if p.kind == PersonKind.STRANGER
            ]

            if self._any_stranger_logged and not current_strangers:
                self._post_wellbeing("unknown", "leave")
                self._any_stranger_logged = False

    def _send_leave_event(self, person_id: str, kind: PersonKind) -> None:
        self._send_event(
            "presence.leave",
            f"Person no longer visible — {kind} ({person_id})",
            "face",
            None,
            config.FACE_COOLDOWN_S,
        )

    def _post_wellbeing(self, user: str, action: str) -> None:
        """POST an enter/leave row to the OS server's wellbeing log.

        Fire-and-forget with a short timeout — a stuck OS server must never
        block face detection. Phase 2 dedup in wellbeing.go absorbs any
        residual duplicates from races or restarts.
        """
        if not user:
            return
        try:
            resp = requests.post(
                config.OS_WELLBEING_LOG_URL,
                json={"action": action, "notes": "", "user": user},
                timeout=2,
            )
            if resp.status_code != 200:
                logger.debug(
                    "[face] wellbeing %s %s returned %d",
                    action,
                    user,
                    resp.status_code,
                )
        except requests.RequestException as e:
            logger.debug("[face] wellbeing %s %s failed: %s", action, user, e)

    # -- Stranger visit tracking -------------------------------------------------

    @staticmethod
    def _load_stranger_stats() -> dict[str, Any]:
        try:
            return json.loads(_STRANGER_STATS_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_stranger_stats(self) -> None:
        with self._state_lock:
            try:
                _STRANGER_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
                _ = _STRANGER_STATS_FILE.write_text(
                    json.dumps(self._stranger_visit_counts, indent=2)
                )
            except OSError as e:
                logger.warning("Failed to save stranger stats: %s", e)

    def _track_stranger_visits(self, stranger_ids: set[str]) -> set[str]:
        """Increment visit count for each stranger seen in this frame.

        Returns the subset of stranger_ids whose visit count just reached
        ``_FAMILIAR_VISIT_THRESHOLD`` on this call (transition fires once).
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        just_familiar: set[str] = set()
        with self._state_lock:
            for sid in stranger_ids:
                rec = self._stranger_visit_counts.get(sid)
                if rec is None:
                    self._stranger_visit_counts[sid] = {
                        "count": 1,
                        "first_seen": now,
                        "last_seen": now,
                    }
                else:
                    rec["count"] += 1
                    rec["last_seen"] = now
                    if rec["count"] == _FAMILIAR_VISIT_THRESHOLD:
                        just_familiar.add(sid)

            if stranger_ids:
                self._save_stranger_stats()
        return just_familiar

    def stranger_stats(self) -> dict[str, Any]:
        """Return visit counts for all tracked stranger IDs."""
        with self._state_lock:
            return self._stranger_visit_counts

    def has_friend_present(self) -> bool:
        """Return True if any friend was seen within the forget interval."""
        with self._state_lock:
            owners = {
                p: d
                for p, d in self._people_data_dict.items()
                if d.kind == PersonKind.FRIEND
            }
            if not owners:
                return False
            now_ts = time.time()
            return any(
                (now_ts - d.last_seen) <= self._owners_forget_ts
                for d in owners.values()
                if d.last_seen is not None
            )

    def current_user(self) -> str:
        """Return the name of the person currently "in front" of the device:
        - Friend with the MOST RECENT session start (enter-after-last-leave)
          among friends still within the forget window.
          Lowercased to match the OS server per-user folder convention.
        - "unknown" if no friend is visible but any stranger was seen within
          the stranger forget window (all strangers collapse to one bucket).
        - Empty string if nobody has been seen recently.
        Sorting by session_start (not last_seen) makes the answer deterministic
        when two friends are both continuously present: whoever entered the
        scene latest wins. last_seen ties at ~now while both remain visible,
        so it can't distinguish them. See docs/plan-presence-logging.md.
        """
        now = time.time()
        last_friend: str | None = None
        last_friend_ts: float | None = None
        have_stranger: bool = False
        with self._state_lock:
            for person_id, person_data in self._people_data_dict.items():
                if person_data.last_seen is None:
                    continue
                if (
                    person_data.kind == PersonKind.STRANGER
                    and (now - person_data.last_seen) <= self._strangers_forget_ts
                ):
                    have_stranger = True

                if person_data.kind != PersonKind.FRIEND:
                    continue
                if (now - person_data.last_seen) > self._owners_forget_ts:
                    continue

                session_start = person_data.last_session_time or person_data.last_seen
                if last_friend_ts is None or last_friend_ts < session_start:
                    last_friend = person_id
                    last_friend_ts = session_start

            if last_friend is not None:
                return self.normalize_label(last_friend)

            if have_stranger:
                return "unknown"

            return ""

    # -- Cooldown state / reset -------------------------------------------------

    def cooldown_state(self) -> dict[str, Any]:
        """Return current cooldown state for all tracked persons."""
        cur_ts = time.time()
        owners = []
        strangers = []
        with self._state_lock:
            for person_id, person_data in self._people_data_dict.items():
                if person_data.last_seen is None:
                    continue

                elapsed = cur_ts - person_data.last_seen
                if person_data.kind == PersonKind.FRIEND:
                    remaining = max(0.0, self._owners_forget_ts - elapsed)
                    kind = person_data.kind
                    owners.append(
                        {
                            "person_id": person_id,
                            "kind": kind,
                            "last_seen_ago": round(elapsed, 1),
                            "cooldown_remaining": round(remaining, 1),
                            "cooldown_total": self._owners_forget_ts,
                        }
                    )
                elif person_data.kind == PersonKind.STRANGER:
                    remaining = max(0.0, self._strangers_forget_ts - elapsed)
                    strangers.append(
                        {
                            "person_id": person_id,
                            "kind": "stranger",
                            "last_seen_ago": round(elapsed, 1),
                            "cooldown_remaining": round(remaining, 1),
                            "cooldown_total": self._strangers_forget_ts,
                        }
                    )

            return {
                "owners": owners,
                "strangers": strangers,
                "owners_forget_s": self._owners_forget_ts,
                "strangers_forget_s": self._strangers_forget_ts,
            }

    def reset_cooldowns(self) -> None:
        """Clear all last-seen timestamps so next detection fires events immediately."""
        with self._state_lock:
            self._people_data_dict.clear()
            _ = self._flush_stranger_buffer(time.time())
            logger.info("Face recognition cooldowns reset")

    # -- Events -----------------------------------------------------------------

    _FACE_COLOR: dict[PersonKind, tuple[int, int, int]] = {
        PersonKind.FRIEND: (0, 255, 0),  # green
        PersonKind.STRANGER: (0, 0, 255),  # red
        PersonKind.UNSURE: (0, 255, 255),  # yellow
    }

    def _annotate_frame(
        self,
        frame: cv2.typing.MatLike,
        faces: list[Face],
    ) -> cv2.typing.MatLike:
        """Draw bounding boxes and labels on a frame copy."""
        annotated = frame.copy()
        for f in faces:
            x1, y1, x2, y2 = f.bbox
            color = self._FACE_COLOR.get(f.kind, (128, 128, 128))
            _ = cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            display_label = f.person_id if f.kind != PersonKind.UNSURE else "unsure"
            _ = cv2.putText(
                annotated,
                display_label,
                (x1, y1 - 6),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1,
                cv2.LINE_AA,
            )
        return annotated

    def _flush_stranger_buffer(
        self, cur_ts: float
    ) -> tuple[list[cv2.typing.MatLike], set[str]]:
        """Flush buffered stranger snapshots if the interval has elapsed.

        Returns ([(frame, annotations), ...], flushed_ids). Empty if not yet time to flush.
        """
        with self._state_lock:
            if (cur_ts - self._last_stranger_flush_ts) < self._stranger_flush_interval:
                return [], set()

            snapshots = copy(self._stranger_snapshots_buffers)
            ids = copy(self._stranger_ids_buffer)
            self._stranger_snapshots_buffers.clear()
            self._stranger_ids_buffer.clear()
            self._last_stranger_flush_ts = cur_ts
            logger.info(
                "[face] flushing %d stranger snapshot(s) for %s", len(snapshots), ids
            )
            return snapshots, ids

    def _send_enter_event(
        self,
        frames: list[cv2.typing.MatLike],
        message: str,
    ) -> None:
        """Send a presence.enter event with annotated snapshots.

        Args:
            frames: List of (raw_frame, annotations) tuples. Each frame is annotated
                with bounding boxes and labels before sending. Includes the current
                frame plus any buffered stranger snapshots from the flush window.
            summary: Human-readable description of who was detected
                (e.g. "friend (alice), stranger (stranger_3)").
        """
        self._send_event(
            "presence.enter",
            message,
            "face",
            frames,
            config.FACE_COOLDOWN_S,
        )
