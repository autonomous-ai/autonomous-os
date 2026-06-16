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
| 1 | Action recognition | UniformerV2 | yes | WS action-analysis |
| 2 | Facial emotion (FER) | POSTER V2 | yes | WS + HTTP emotion |
| 3 | Pose estimation | RTMPose + TCPFormer | yes | WS pose |
| 4 | Speech emotion (SER) | emotion2vec | yes | HTTP ser |
| 5 | Object detection | per-detector | no (opt-in) | WS + HTTP object |
| 6 | Audio embedder | WeSpeaker ECAPA-TDNN-1024 | no | HTTP embed |
| 7 | Face detection | YuNet | (internal) | feeds FER/pose |
| 8 | Person detection | YOLO | no (internal) | feeds action |

---

## 1. Action recognition

Classifies human actions from a rolling clip of frames into Kinetics classes.

- Enum `HumanActionRecognizerEnum` (`enums/action.py`): `x3d`, `uniformerv2`, `videomae`
- Predictors (`perception/action/predictors/`):

  | Model | File | Architecture | Weights | Input | Frames | Classes |
  |-------|------|-------------|---------|-------|--------|---------|
  | **UniformerV2** (default) | `uniformerv2.py` | UniformerV2-L ONNX | `uniformerv2-l-224-k400_fp32.onnx` | 224×224 | 8 | Kinetics |
  | X3D | `x3d.py` | X3D-M ONNX (INT8) | `x3d_m_16x5x1_int8.onnx` | 256×256 | 16 | Kinetics |
  | VideoMAE | `videomae.py` | VideoMAE ONNX | `videomae_fp32.onnx` | 224×224 | 16 | Kinetics |

- Output: `HumanActionDetection` → `actions: list[HumanAction{class_name, conf}]`.
- A per-request `whitelist` filters which classes are scored; optional YOLO person
  detection crops the largest person first (helps under camera ego-motion).

## 2. Facial emotion (FER)

Classifies emotion from a face crop.

- Enum `EmotionRecognizerEnum` (`enums/facial_emotion.py`): `posterv2`, `emonet_8`, `emonet_5`
- Predictors (`perception/facial_emotion/predictors/`):

  | Model | File | Architecture | Weights | Input | Output |
  |-------|------|-------------|---------|-------|--------|
  | **POSTER V2** (default) | `posterv2.py` | POSTER V2 ONNX | `posterv2_7cls.onnx` | 224×224 | 7 RAF-DB emotions |
  | EmoNet-8 | `emonet.py` | EmoNet ONNX | `emonet_8.onnx` | 256×256 | 8 emotions + valence + arousal |
  | EmoNet-5 | `emonet.py` | EmoNet ONNX | `emonet_5.onnx` | 256×256 | 5 emotions + valence + arousal |

- Output: `EmotionDetection` → `emotions: list[Emotion{emotion, confidence,
  face_confidence, bbox, valence?, arousal?}]`.
- The WS endpoint detects faces with **YuNet** (subsystem 7) before classifying;
  the HTTP endpoint expects an already-cropped face.

## 3. Pose estimation

A 3-stage pipeline: 2D keypoints → optional 3D lift → optional RULA ergonomics.

- Enums (`enums/pose.py`): `GraphEnum{coco, h36m}`,
  `PoseEstimator2DEnum{rtmpose}`, `PoseLifter3DEnum{tcpformer}`, `ErgoAssessorEnum{rula}`

| Stage | Model | File | Weights | Notes |
|-------|-------|------|---------|-------|
| 2D | **RTMPose-M** | `pose/predictors/pose2d/rtmpose.py` | `rtmpose-m.onnx` | 192×256 input, COCO-17, SimCC x/y decode |
| 3D lift | **TCPFormer** | `pose/predictors/pose3d/tcpformer.py` | `tcpformer_h36m_243.onnx` | H36M-17, 243-frame temporal window |
| Ergonomics | **RULA** | `pose/predictors/ergo/rula/assessor.py` | (rule-based) | Rapid Upper Limb Assessment |

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
  large**, ONNX (`emotion2vec.onnx`), mono 16 kHz waveform, 9 classes (angry, disgusted, fearful, happy,
  neutral, other, sad, surprised, `<unk>`) + softmax.
- Output `AudioEmotionDetection` → `emotions: list[AudioEmotion{emotion, confidence}]`.
- The audio processor can resample, high-pass, denoise, VAD-gate and RMS-normalize
  before inference (toggles under `AUDIO_EMBEDDER__*` / SER processor config).

## 5. Object detection

Open-vocabulary / zero-shot detection. Each detector is independently enabled and
selected by URL path segment (`{detector_name}`).

- Enum `ObjectDetectorEnum` (`enums/object.py`): `yoloworld`, `yoloe`, `owlv2`, `grounding-dino`
- Predictors (`perception/object/predictors/`):

  | Detector | Enum value | File | Backend | Default weights | GPU |
  |----------|-----------|------|---------|-----------------|-----|
  | YOLO-World | `yoloworld` | `yolo_world.py` | Ultralytics `.pt` | `yolov8x-worldv2.pt` | CUDA / CPU |
  | YOLOE | `yoloe` | `yoloe.py` | Ultralytics `.pt` | `yoloe-26x-seg.pt` | CUDA / CPU |
  | OWLv2 | `owlv2` | `owlv2.py` | HF Transformers | `google/owlv2-large-patch14-ensemble` | CUDA / CPU |
  | Grounding DINO | `grounding-dino` | `grounding_dino.py` | HF Transformers | `IDEA-Research/grounding-dino-tiny` | CUDA / CPU |

- Output `ObjectDetection` → items `{class_name, xywh:[cx,cy,w,h], confidence}`.
- `classes` is supplied per request (the open-vocabulary prompt). All detectors are
  **disabled by default** — enable the ones you need via `OBJECT_DETECTOR__<NAME>__ENABLED`.

## 6. Audio embedder (speaker)

Produces a speaker embedding for verification/identification (matching is done by
the caller).

- Enum `AudioEmbedderEnum` (`enums/audio.py`): `resnet34`, `ecapa-tdnn1024`, `campplus`
- Predictors (`perception/audio/predictors/`):

  | Model | File | Weights | Embedding dim |
  |-------|------|---------|---------------|
  | WeSpeaker ResNet34 | `resnet34.py` | `wespeaker_resnet34.onnx` | 256 |
  | **WeSpeaker ECAPA-TDNN-1024** (default) | `ecapa_tdnn.py` | `wespeaker_ecapa_tdnn1024.onnx` | 1024 |
  | WeSpeaker CAM++ | `campplus.py` | `wespeaker_campplus.onnx` | — |

- All models: 16 kHz mono input → 80-bin fbank → 2 s sliding windows (50 % overlap)
  → L2-normalized embedding.
- Output `RawAudioEmbedding{embedding, chunk_embeddings}`. Disabled by default.

## 7. Face detection (internal)

- Enum `FaceDetectorEnum` (`enums/face.py`): `yunet`
- Predictor `perception/face/predictors/yunet.py` — OpenCV `FaceDetectorYN`,
  320×320, score ≥ 0.7, NMS 0.3. Returns `RawFaceDetection{bbox_xyxy, confidence,
  area}` and can `extract_crops()`. Used by the facial-emotion WS pipeline.

## 8. Person detection (internal)

- Enum `PersonDetectorEnum` (`enums/person.py`): `yolo`
- Predictor `perception/person/predictors/yolo.py` — Ultralytics YOLO (`yolo12x.pt`)
  filtered to COCO class 0, confidence 0.4, bbox expanded 2.0×. `extract_largest_crop()`
  feeds the action recognizer. Disabled by default (`PERSON_DETECTOR__ENABLED=false`).

---

## Model weights reference

All ONNX/PyTorch weights are auto-downloaded from
`https://storage.googleapis.com/autonomous-models/` into `~/.cache/dlbackend/models/`
on first use. Override with `<NAME>__CKPT_PATH` (local) or `<NAME>__REMOTE_URL` (alternate CDN/HF).

| Model | Weights file | Format | CDN path |
|-------|-------------|--------|----------|
| UniformerV2-L | `uniformerv2-l-224-k400_fp32.onnx` | ONNX FP32 | `onnx_models/` |
| X3D-M | `x3d_m_16x5x1_int8.onnx` | ONNX INT8 | `onnx_models/` |
| VideoMAE | `videomae_fp32.onnx` | ONNX FP32 | `onnx_models/` |
| POSTER V2 | `posterv2_7cls.onnx` | ONNX | `onnx_models/` |
| EmoNet-8 | `emonet_8.onnx` | ONNX | `onnx_models/` |
| EmoNet-5 | `emonet_5.onnx` | ONNX | `onnx_models/` |
| RTMPose-M | `rtmpose-m.onnx` | ONNX | `onnx_models/` |
| TCPFormer | `tcpformer_h36m_243.onnx` | ONNX | `onnx_models/` |
| emotion2vec+ | `emotion2vec.onnx` | ONNX | `onnx_models/` |
| ECAPA-TDNN-1024 | `wespeaker_ecapa_tdnn1024.onnx` | ONNX | `onnx_models/` |
| ResNet34 | `wespeaker_resnet34.onnx` | ONNX | `onnx_models/` |
| CAM++ | `wespeaker_campplus.onnx` | ONNX | `onnx_models/` |
| YuNet | `face_detection_yunet_2023mar.onnx` | ONNX | `onnx_models/` |
| YOLO person | `yolo12x.pt` | PyTorch | `pytorch_models/` |
| YOLO-World v2 | `yolov8x-worldv2.pt` | PyTorch | `pytorch_models/` |
| OWLv2 | `google/owlv2-large-patch14-ensemble` | HuggingFace | (HF Hub) |
| Grounding DINO | `IDEA-Research/grounding-dino-tiny` | HuggingFace | (HF Hub) |
</content>
