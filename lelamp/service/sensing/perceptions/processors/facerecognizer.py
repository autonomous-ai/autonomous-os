import json
import logging
import re
import shutil
import threading
import time
from copy import copy
from pathlib import Path
from typing import Any, Callable, override

import cv2
import insightface
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
import requests

import lelamp.config as config
from lelamp.service.sensing.perceptions.models import (
    Face,
    FaceDetectionData,
    PersonData,
    PersonKind,
)
from lelamp.service.sensing.perceptions.typing import SendEventCallable
from lelamp.service.sensing.perceptions.utils import PerceptionStateObservers
from lelamp.service.sensing.presence_service import PresenseService

from .base import Perception

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

_NO_MATCH = -2.0  # sentinel score used when an embedding bank is empty

# Per-user data directory (face photos, wellbeing notes, mood history)
USERS_DIR = Path(config.USERS_DIR)
USERS_DIR.mkdir(parents=True, exist_ok=True)
STRANGER_STATE_DIR = Path(config.STRANGERS_DIR)
STRANGER_STATE_DIR.mkdir(exist_ok=True, parents=True)
_STRANGER_STATS_FILE = USERS_DIR / ".stranger_stats.json"
_STRANGER_SNAPSHOTS_DIR = STRANGER_STATE_DIR / "snapshots"

# Visit count at which lelamp prompts the user to enroll a familiar stranger.
# Fires exactly once per stranger when count first reaches this value; the
# face-enroll skill handles asking the user and POST /face/enroll on confirm.
_FAMILIAR_VISIT_THRESHOLD = 2


class FaceRecognizer:
    FRIEND_PREFIX: str = "friend_"
    STRANGER_PREFIX: str = "stranger_"

    def __init__(
        self,
        area_ratio_threshold: float = config.FACE_AREA_RATIO_THRESHOLD,
        threshold: float = 0.4,
        negative_threshold: float | None = 0.2,
        max_strangers: int = 50,
        model_name: str = "buffalo_sc",
    ):
        self._area_ratio_threshold: float = area_ratio_threshold
        self._threshold: float = threshold
        self._negative_threshold: float | None = negative_threshold
        self._max_strangers: int = max_strangers
        self._model_name: str = model_name

        self._app: insightface.app.FaceAnalysis | None = None
        self._owner_embeddings: npt.NDArray[np.float32] | None = None
        self._owner_labels: npt.NDArray[np.str_] | None = None
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

        self._app = insightface.app.FaceAnalysis(
            name=self._model_name, session_options=sess_opts
        )
        self._app.prepare(ctx_id=-1)

    def reset(self, owners: bool = True, strangers: bool = True):
        with self._lock:
            if owners:
                self._owner_embeddings = None
                self._owner_labels = None

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

            owner_scores, owner_ids = self._retrieve(
                embeds, self._owner_embeddings, self._owner_labels
            )
            stranger_scores, stranger_ids = self._retrieve(
                embeds, self._stranger_embeddings, self._stranger_labels
            )

        new_stranger_embeds = []
        new_stranger_labels = []
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
    """InsightFace-based face recognizer. Detects friends and strangers, fires presence events."""

    FRIEND_PREFIX: str = "friend_"
    STRANGER_PREFIX: str = "stranger_"

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        presense_service: PresenseService | None = None,
        threshold: float = 0.4,
        negative_threshold: float | None = 0.2,
        max_strangers: int = 50,
        model_name: str = "buffalo_sc",
        area_ratio_threshold: float = config.FACE_AREA_RATIO_THRESHOLD,
        owners_forget_ts: float = config.FACE_OWNER_FORGET_S,
        strangers_forget_ts: float = config.FACE_STRANGER_FORGET_S,
    ):
        super().__init__(perception_state, send_event)

        self._presense_service: PresenseService | None = presense_service
        self._face_recognizer: FaceRecognizer = FaceRecognizer(
            area_ratio_threshold=area_ratio_threshold,
            threshold=threshold,
            negative_threshold=negative_threshold,
            max_strangers=max_strangers,
            model_name=model_name,
        )
        self._face_recognizer.start()
        self._owners_forget_ts: float = owners_forget_ts
        self._strangers_forget_ts: float = strangers_forget_ts

        self._faces_n: int = 0
        self._face_present: bool = False
        self._people_data_dict: dict[str, PersonData] = {}
        self._owners: set[str] = set()
        self._strangers: set[str] = set()

        # self._known_face_kinds: dict[str, str] = {}  # person_id → "friend"
        # self._owners_last_seen: dict[str, float] = {}
        # self._strangers_last_seen: dict[str, float] = {}
        # # Session-start = timestamp the person re-entered the scene after the
        # # previous leave (or first-ever detection). Unlike last_seen (updated
        # # every frame), this is set once on "fresh" detection and cleared on
        # # leave — so current_user() can pick the right friend when two are
        # # continuously present. See docs/plan-presence-logging.md.
        # self._owners_session_start: dict[str, float] = {}
        # self._strangers_session_start: dict[str, float] = {}
        # # True between the first stranger-enter row LeLamp has posted to the
        # # wellbeing log and the corresponding "all strangers gone" leave.
        # # Keeps the "unknown" timeline as a single session even as different
        # # stranger IDs cycle in and out — without this flag, stranger_37 →
        # # stranger_38 while both are within the forget window would never
        # # produce a matching leave, and re-enters would stack duplicate rows.
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
            try:
                return max(
                    (e.stat().st_mtime for e in USERS_DIR.rglob("*")),
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
        """Clear enrolled embeddings and re-train from all JPEG/PNG images under USERS_DIR."""
        self._clear_owner_embeddings()

        if not USERS_DIR.is_dir():
            logger.info("No users dir at %s — skipping", USERS_DIR)
            return 0

        _IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
        loaded_total = 0

        for person_dir in sorted(USERS_DIR.iterdir()):
            if not person_dir.is_dir():
                continue

            images: list[cv2.typing.MatLike] = []
            labels: list[str] = []
            for fname in sorted(person_dir.iterdir()):
                if fname.suffix.lower() not in _IMG_EXTS:
                    continue
                img = cv2.imread(str(fname))
                if img is None:
                    logger.warning("Failed to load image: %s", fname)
                    continue
                images.append(img)
                labels.append(person_dir.name)

            if images:
                self.train(images, labels)
                loaded_total += len(images)
                logger.info(
                    "Loaded %d image(s) for '%s'",
                    len(images),
                    person_dir.name,
                )

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
                        # Per-friend enter row: Lamp's wellbeing_context uses
                        # "enter" as one of the reset anchors for hydration/break
                        # deltas; without it, deltas stay -1 all day and
                        # nudge_hydration never fires for a user who hasn't been
                        # caught drinking by the camera yet.
                        self._post_wellbeing(
                            self.normalize_label(person_id), "enter"
                        )
                    elif face_data.kind == PersonKind.STRANGER:
                        new_strangers.add(person_id)

                    self._people_data_dict[person_id].last_session_time = cur_ts

                self._people_data_dict[person_id].last_seen = cur_ts

            # "unknown" session enter: fire once when the first stranger appears
            # and stays un-logged until all strangers leave. Keeps multiple
            # stranger IDs (stranger_37 → stranger_38 → stranger_52) collapsed
            # into one session row for the "unknown" user timeline.
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
                # Familiar-stranger prompt: fires exactly once per stranger
                # at visit count == _FAMILIAR_VISIT_THRESHOLD. Skill parses
                # this hint to ask the user whether to enroll the face.
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

            # "unknown" session leave: fire once when the last stranger has
            # gone. Mirrors the enter in _check_impl — gives the unknown
            # timeline matching enter/leave pairs even though individual
            # stranger IDs don't emit presence.leave.
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
        """POST an enter/leave row to Lamp's wellbeing log.

        Fire-and-forget with a short timeout — a stuck Lamp must never
        block face detection. Phase 2 dedup in wellbeing.go absorbs any
        residual duplicates from races or restarts.
        """
        if not user:
            return
        try:
            resp = requests.post(
                config.LAMP_WELLBEING_LOG_URL,
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
        """Return the name of the person currently "in front" of the lamp:
        - Friend with the MOST RECENT session start (enter-after-last-leave)
          among friends still within the forget window.
          Lowercased to match the Lamp per-user folder convention.
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
