# Perception Subsystems

All perceptions live under `src/core/perception/<name>` and follow the same shape:

- a **predictor** (`predictors/`) wraps the ML model (ONNX / PyTorch / HF) and
  implements `PredictorBase[INPUT, OUTPUT]` (`src/core/perception/base/`),
- a **perception session** manages per-connection state and batching,
- a **factory** picks the concrete predictor from an **enum** (`src/core/enums/`),
- result **data models** live in `src/core/models/`.

The configured default for each subsystem comes from `src/config.py`
(`<NAME>__MODEL`, `<NAME>__ENABLED`), summarized in
[configuration.md](configuration.md).

| # | Subsystem | Default model | Enabled by default | Routes |
|---|-----------|---------------|--------------------|--------|
| 1 | Action recognition | X3D | yes | WS action-analysis |
| 2 | Facial emotion (FER) | POSTER V2 | yes | WS + HTTP emotion |
| 3 | Pose estimation | RTMPose + TCPFormer | yes | WS pose |
| 4 | Speech emotion (SER) | emotion2vec | yes | HTTP ser |
| 5 | Object detection | per-detector | no (opt-in) | WS + HTTP object |
| 6 | Audio embedder | WeSpeaker ResNet34 | no | HTTP embed |
| 7 | Face detection | YuNet | (internal) | feeds FER/pose |
| 8 | Person detection | YOLO | no (internal) | feeds action |

---

## 1. Action recognition

Classifies human actions from a rolling clip of frames into Kinetics classes.

- Enum `HumanActionRecognizerEnum` (`enums/action.py`): `x3d`, `uniformerv2`, `videomae`
- Predictors (`perception/action/predictors/`):

  | Model | File | Input | Frames | Classes |
  |-------|------|-------|--------|---------|
  | **X3D** (default) | `x3d.py` | 256×256 | 16 | Kinetics (`kinect_classes.txt`) |
  | UniformerV2 | `uniformerv2.py` | 224×224 | 8 | Kinetics |
  | VideoMAE | `videomae.py` | 224×224 | 16 | Kinetics |

- Output: `HumanActionDetection` → `actions: list[HumanAction{class_name, conf}]`.
- A per-request `whitelist` filters which classes are scored; optional YOLO person
  detection crops the largest person first (helps under camera ego-motion).

## 2. Facial emotion (FER)

Classifies emotion from a face crop.

- Enum `EmotionRecognizerEnum` (`enums/facial_emotion.py`): `posterv2`, `emonet_8`, `emonet_5`
- Predictors (`perception/facial_emotion/predictors/`):

  | Model | File | Input | Output |
  |-------|------|-------|--------|
  | **POSTER V2** (default) | `posterv2.py` | 224×224 | 7 RAF-DB emotions |
  | EmoNet-8 | `emonet.py` | 256×256 | 8 emotions + valence + arousal |
  | EmoNet-5 | `emonet.py` | 256×256 | 5 emotions + valence + arousal |

- Output: `EmotionDetection` → `emotions: list[Emotion{emotion, confidence,
  face_confidence, bbox, valence?, arousal?}]`.
- The WS endpoint detects faces with **YuNet** (subsystem 7) before classifying;
  the HTTP endpoint expects an already-cropped face.

## 3. Pose estimation

A 3-stage pipeline: 2D keypoints → optional 3D lift → optional RULA ergonomics.

- Enums (`enums/pose.py`): `GraphEnum{coco, h36m}`,
  `PoseEstimator2DEnum{rtmpose}`, `PoseLifter3DEnum{tcpformer}`, `ErgoAssessorEnum{rula}`

| Stage | Model | File | Notes |
|-------|-------|------|-------|
| 2D | **RTMPose** | `pose/predictors/pose2d/rtmpose.py` | 192×256 input, COCO-17, SimCC x/y decode |
| 3D lift | **TCPFormer** | `pose/predictors/pose3d/tcpformer.py` | H36M-17, 243-frame temporal window |
| Ergonomics | **RULA** | `pose/predictors/ergo/rula/assessor.py` | Rapid Upper Limb Assessment |

- 2D output `Pose2D{graph_type, joints:[Point2D], confs}`; 3D output
  `Pose3D{graph_type, joints:[Point3D], confs}`; combined `PoseDetection{pose_2d,
  pose_3d?, ergo?}`.
- **RULA** scores upper-limb and trunk/neck/leg posture from the 3D skeleton and
  maps to a `RiskLevel` (`models/pose.py`): `NEGLIGIBLE(1)`, `LOW(2)`, `MEDIUM(3)`,
  `HIGH(4)` — the standard ergonomics escalation for "how urgently should this
  posture change". The 3D lift and ergo stages only run when enabled in config.

## 4. Speech emotion (SER)

Classifies emotion from a speech waveform (independent of any transcript).

- Enum `SpeechEmotionRecognizerEnum` (`enums/speech_emotion_recognizer.py`): `emotion2vec`
- Predictor `perception/audio_emotion/predictors/emotion2vec.py` — **emotion2vec+
  large**, ONNX, mono 16 kHz waveform, 9 classes (angry, disgusted, fearful, happy,
  neutral, other, sad, surprised, `<unk>`) + softmax.
- Output `AudioEmotionDetection` → `emotions: list[AudioEmotion{emotion, confidence}]`.
- The audio processor can resample, high-pass, denoise, VAD-gate and RMS-normalize
  before inference (toggles under `AUDIO_EMBEDDER__*` / SER processor config).

## 5. Object detection

Open-vocabulary / zero-shot detection. Each detector is independently enabled and
selected by URL path segment (`{detector_name}`).

- Enum `ObjectDetectorEnum` (`enums/object.py`): `yoloworld`, `yoloe`, `owlv2`, `grounding-dino`
- Predictors (`perception/object/predictors/`):

  | Detector | File | Backend | Default weights |
  |----------|------|---------|-----------------|
  | YOLO-World | `yolo_world.py` | Ultralytics `.pt` | `yolov8s-worldv2.pt` |
  | YOLOE | `yoloe.py` | Ultralytics `.pt` | `yoloe-26x-seg.pt` |
  | OWLv2 | `owlv2.py` | HF Transformers | `google/owlv2-large-patch14-ensemble` |
  | Grounding DINO | `grounding_dino.py` | HF Transformers | `IDEA-Research/grounding-dino-tiny` |

- Output `ObjectDetection` → items `{class_name, xywh:[cx,cy,w,h], confidence}`.
- `classes` is supplied per request (the open-vocabulary prompt). All detectors are
  **disabled by default** — enable the ones you need via `OBJECT_DETECTOR__<NAME>__ENABLED`.

## 6. Audio embedder (speaker)

Produces a speaker embedding for verification/identification (matching is done by
the caller).

- Enum `AudioEmbedderEnum` (`enums/audio.py`): `resnet34`, `ecapa-tdnn1024`, `campplus`
- Predictors (`perception/audio/predictors/`): WeSpeaker ONNX models — `resnet34.py`
  (default), `ecapa_tdnn.py` (1024-dim), `campplus.py`. 16 kHz input → 80-bin fbank
  → 2 s sliding windows (50 % overlap) → L2-normalized embedding.
- Output `RawAudioEmbedding{embedding, chunk_embeddings}`. Disabled by default.

## 7. Face detection (internal)

- Enum `FaceDetectorEnum` (`enums/face.py`): `yunet`
- Predictor `perception/face/predictors/yunet.py` — OpenCV `FaceDetectorYN`,
  320×320, score ≥ 0.7, NMS 0.3. Returns `RawFaceDetection{bbox_xyxy, confidence,
  area}` and can `extract_crops()`. Used by the facial-emotion WS pipeline.

## 8. Person detection (internal)

- Enum `PersonDetectorEnum` (`enums/person.py`): `yolo`
- Predictor `perception/person/predictors/yolo.py` — Ultralytics YOLO filtered to
  COCO class 0, confidence 0.4, bbox expanded 2.0×. `extract_largest_crop()` feeds
  the action recognizer. Disabled by default (`PERSON_DETECTOR__ENABLED=false`).
</content>
