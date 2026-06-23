"""Tests for AudioEmotionRecognizer and AudioEmotionPerception with real emotion2vec model."""

import asyncio
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.enums import SpeechEmotionRecognizerEnum
from core.enums.files import ModelEnum
from core.models.audio_emotion import AudioEmotionDetection, AudioEmotionPerceptionSessionConfig
from core.models.media import Audio
from core.perception.audio_emotion.perception import AudioEmotionPerception
from core.perception.audio_emotion.predictors.emotion2vec import Emotion2VecPlusLargeRecognizer
from core.perception.audio_emotion.utils import AudioEmotionRecognizerFactory
from core.utils.files import get_default_model_path

MODEL_PATH = get_default_model_path(ModelEnum.EMOTION2VEC_ONNX)
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "audio"

pytestmark = pytest.mark.skipif(
    MODEL_PATH is None or not FIXTURES_DIR.exists(),
    reason="Model or audio fixtures not found",
)


def _load_wav(path: Path) -> Audio:
    waveform, sr = sf.read(str(path), dtype="float32")
    waveform = np.asarray(waveform, dtype=np.float32)
    if waveform.ndim == 2:
        waveform = waveform.mean(axis=1)
    return Audio(waveform=waveform, sample_rate=int(sr))


@pytest.fixture(scope="module")
def recognizer():
    rec = Emotion2VecPlusLargeRecognizer(model_path=MODEL_PATH)
    rec.start()
    yield rec
    rec.stop()


@pytest.fixture(scope="module")
def perception():
    factory = AudioEmotionRecognizerFactory(
        model_name=SpeechEmotionRecognizerEnum.EMOTION2VEC,
        model_path=MODEL_PATH,
    )
    p = AudioEmotionPerception(audio_emotion_recognizer_factory=factory)
    asyncio.run(p.start())
    yield p
    asyncio.run(p.stop())


@pytest.fixture(scope="module")
def happy_audio():
    return _load_wav(FIXTURES_DIR / "happy.wav")


@pytest.fixture(scope="module")
def sad_audio():
    return _load_wav(FIXTURES_DIR / "sad.wav")


class TestRecognizerPrediction:
    def test_predict_returns_correct_shape(self, recognizer, happy_audio):
        results = recognizer.predict([happy_audio])
        assert len(results) == 1
        assert results[0].expression_probs.ndim == 1
        assert results[0].expression_probs.shape[0] == len(recognizer.class_names)

    def test_predict_batch(self, recognizer, happy_audio, sad_audio):
        results = recognizer.predict([happy_audio, sad_audio])
        assert len(results) == 2

    def test_probs_sum_to_one(self, recognizer, happy_audio):
        results = recognizer.predict([happy_audio])
        total = float(results[0].expression_probs.sum())
        assert abs(total - 1.0) < 1e-2

    def test_happy_detected(self, recognizer, happy_audio):
        """'happy' should be the top emotion with high confidence."""
        results = recognizer.predict([happy_audio])
        probs = results[0].expression_probs
        ranked = np.argsort(probs)[::-1]
        top_names = [recognizer.class_names[i] for i in ranked if recognizer.class_names[i] != "<unk>"]
        assert top_names[0] == "happy", f"Expected 'happy' as top-1, got: {top_names[0]}"
        happy_idx = recognizer.class_names.index("happy")
        assert float(probs[happy_idx]) > 0.5, f"Happy confidence too low: {probs[happy_idx]:.3f}"

    def test_sad_detected(self, recognizer, sad_audio):
        """'sad' should be the top emotion."""
        results = recognizer.predict([sad_audio])
        probs = results[0].expression_probs
        ranked = np.argsort(probs)[::-1]
        top_names = [recognizer.class_names[i] for i in ranked if recognizer.class_names[i] != "<unk>"]
        assert top_names[0] == "sad", f"Expected 'sad' as top-1, got: {top_names[0]}"


class TestPerceptionPrediction:
    def test_predict_audio_returns_detection(self, perception, happy_audio):
        detection = asyncio.run(perception.predict_audio(happy_audio))
        assert isinstance(detection, AudioEmotionDetection)
        assert len(detection.emotions) > 0

    def test_predict_audio_sorted_by_confidence(self, perception, happy_audio):
        detection = asyncio.run(perception.predict_audio(happy_audio))
        confidences = [e.confidence for e in detection.emotions]
        assert confidences == sorted(confidences, reverse=True)

    def test_scores_sum_to_one(self, perception, happy_audio):
        detection = asyncio.run(perception.predict_audio(happy_audio))
        total = sum(e.confidence for e in detection.emotions)
        assert abs(total - 1.0) < 1e-4

    def test_predict_audio_happy(self, perception, happy_audio):
        """'happy' should be the top emotion with high confidence."""
        detection = asyncio.run(perception.predict_audio(happy_audio))
        top = [e for e in detection.emotions if e.emotion != "<unk>"]
        assert top[0].emotion == "happy", f"Expected 'happy' as top-1, got: {top[0].emotion}"
        assert top[0].confidence > 0.5, f"Happy confidence too low: {top[0].confidence:.3f}"

    def test_predict_audio_sad(self, perception, sad_audio):
        """'sad' should be the top emotion."""
        detection = asyncio.run(perception.predict_audio(sad_audio))
        top = [e for e in detection.emotions if e.emotion != "<unk>"]
        assert top[0].emotion == "sad", f"Expected 'sad' as top-1, got: {top[0].emotion}"

    def test_batch_both_detected(self, perception, happy_audio, sad_audio):
        """Both happy and sad should be top-1 for their respective audio."""
        happy_det = asyncio.run(perception.predict_audio(happy_audio))
        sad_det = asyncio.run(perception.predict_audio(sad_audio))
        happy_top = [e for e in happy_det.emotions if e.emotion != "<unk>"][0]
        sad_top = [e for e in sad_det.emotions if e.emotion != "<unk>"][0]
        assert happy_top.emotion == "happy", f"Expected 'happy', got: {happy_top.emotion}"
        assert sad_top.emotion == "sad", f"Expected 'sad', got: {sad_top.emotion}"


class TestSession:
    def test_session_update(self, perception, happy_audio):
        session = asyncio.run(perception.create_session())
        asyncio.run(session.start())
        result = asyncio.run(session.update(happy_audio))
        assert result is not None
        assert len(result.emotions) > 0
        top = [e for e in result.emotions if e.emotion != "<unk>"][0]
        assert top.emotion == "happy", f"Expected 'happy' as top-1, got: {top.emotion}"

    def test_session_threshold_filters(self, perception, happy_audio):
        config = AudioEmotionPerceptionSessionConfig(confidence_threshold=0.99)
        session = asyncio.run(perception.create_session())
        session.set_config(config)
        asyncio.run(session.start())
        result = asyncio.run(session.update(happy_audio))
        assert result is not None
        # With very high threshold, most classes should be filtered
        assert len(result.emotions) < len(perception.labels)


class TestRejection:
    def test_predict_before_start_raises(self, happy_audio):
        rec = Emotion2VecPlusLargeRecognizer(model_path=MODEL_PATH)
        with pytest.raises(RuntimeError, match="not ready"):
            rec.predict([happy_audio])

    def test_predict_after_stop_raises(self, happy_audio):
        rec = Emotion2VecPlusLargeRecognizer(model_path=MODEL_PATH)
        rec.start()
        rec.stop()
        with pytest.raises(RuntimeError, match="not ready"):
            rec.predict([happy_audio])

    def test_perception_predict_before_start_raises(self, happy_audio):
        factory = AudioEmotionRecognizerFactory(
            model_name=SpeechEmotionRecognizerEnum.EMOTION2VEC,
            model_path=MODEL_PATH,
        )
        p = AudioEmotionPerception(audio_emotion_recognizer_factory=factory)
        with pytest.raises(RuntimeError):
            asyncio.run(p.predict_audio(happy_audio))
