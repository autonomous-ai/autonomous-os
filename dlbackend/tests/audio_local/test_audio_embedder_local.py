"""Tests for AudioEmbedder with real WeSpeaker ResNet34 model."""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.enums.files import ModelEnum
from core.models.media import Audio
from core.perception.audio.predictors.base import AudioEmbedder
from core.perception.audio.processors.exceptions import PreprocessRejected
from core.perception.audio.processors.utils import AudioProcessorFactory
from core.perception.audio.processors.voice_activity_filter import VoiceActivityFilter
from core.utils.files import get_default_model_path

MODEL_PATH = get_default_model_path(ModelEnum.WESPEAKER_RESNET34)

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
def embedder():
    proc_factory = AudioProcessorFactory(
        enable_resample=False,
        enable_high_pass=False,
        enable_noise_reduce=False,
        enable_vad=False,
        enable_rms_normalize=False,
    )
    emb = AudioEmbedder(model_path=MODEL_PATH, processor_factory=proc_factory)
    emb.start()
    yield emb
    emb.stop()


@pytest.fixture(scope="module")
def speaker_a_audios():
    return [_load_wav(f) for f in sorted((FIXTURES_DIR / "speaker_a").glob("*.wav"))]


@pytest.fixture(scope="module")
def speaker_b_audios():
    return [_load_wav(f) for f in sorted((FIXTURES_DIR / "speaker_b").glob("*.wav"))]


@pytest.fixture(scope="module")
def soundscape_audio():
    return _load_wav(FIXTURES_DIR / "soundscape.wav")


class TestEmbeddingShape:
    def test_single_audio(self, embedder, speaker_a_audios):
        results = embedder.predict([speaker_a_audios[0]])
        assert len(results) == 1
        assert results[0].embedding.ndim == 1
        assert results[0].embedding.shape[0] > 0
        assert results[0].chunk_embeddings.ndim == 2
        assert results[0].chunk_embeddings.shape[1] == results[0].embedding.shape[0]

    def test_batch(self, embedder, speaker_a_audios, speaker_b_audios):
        batch = speaker_a_audios[:1] + speaker_b_audios[:1]
        results = embedder.predict(batch)
        assert len(results) == 2

    def test_l2_normalized(self, embedder, speaker_a_audios):
        results = embedder.predict(speaker_a_audios)
        for r in results:
            assert abs(np.linalg.norm(r.embedding) - 1.0) < 1e-5
            norms = np.linalg.norm(r.chunk_embeddings, axis=1)
            assert np.allclose(norms, 1.0, atol=1e-5)


class TestAggregation:
    def test_mean_aggregate_matches(self, embedder, speaker_a_audios):
        result = embedder.predict([speaker_a_audios[0]])[0]
        manual_mean = result.chunk_embeddings.mean(axis=0)
        manual_agg = manual_mean / np.linalg.norm(manual_mean)
        assert np.allclose(result.embedding, manual_agg, atol=1e-6)


class TestSpeakerDiscrimination:
    def _cosine(self, a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b))

    def test_same_person_higher_than_different(self, embedder, speaker_a_audios, speaker_b_audios):
        a_embs = [r.embedding for r in embedder.predict(speaker_a_audios)]
        b_embs = [r.embedding for r in embedder.predict(speaker_b_audios)]

        same_sims = []
        for i in range(len(a_embs)):
            for j in range(i + 1, len(a_embs)):
                same_sims.append(self._cosine(a_embs[i], a_embs[j]))
        for i in range(len(b_embs)):
            for j in range(i + 1, len(b_embs)):
                same_sims.append(self._cosine(b_embs[i], b_embs[j]))

        diff_sims = []
        for a in a_embs:
            for b in b_embs:
                diff_sims.append(self._cosine(a, b))

        avg_same = np.mean(same_sims)
        avg_diff = np.mean(diff_sims)
        assert avg_same > avg_diff, (
            f"Same-person avg ({avg_same:.4f}) should exceed "
            f"different-person avg ({avg_diff:.4f})"
        )

    def test_soundscape_lower_than_same_person(
        self, embedder, speaker_a_audios, speaker_b_audios, soundscape_audio
    ):
        sc_emb = embedder.predict([soundscape_audio])[0].embedding
        speaker_embs = [
            r.embedding for r in embedder.predict(speaker_a_audios + speaker_b_audios)
        ]

        avg_sc = np.mean([self._cosine(sc_emb, e) for e in speaker_embs])

        a_embs = [r.embedding for r in embedder.predict(speaker_a_audios)]
        same_sims = []
        for i in range(len(a_embs)):
            for j in range(i + 1, len(a_embs)):
                same_sims.append(self._cosine(a_embs[i], a_embs[j]))
        avg_same = np.mean(same_sims)

        assert avg_sc < avg_same, (
            f"Soundscape avg ({avg_sc:.4f}) should be lower than "
            f"same-person avg ({avg_same:.4f})"
        )


class TestChunkEmbeddings:
    def _cosine(self, a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b))

    def test_chunk_count_scales_with_duration(self, embedder, speaker_a_audios, soundscape_audio):
        short_result = embedder.predict([speaker_a_audios[0]])[0]
        long_result = embedder.predict([soundscape_audio])[0]
        assert long_result.chunk_embeddings.shape[0] > short_result.chunk_embeddings.shape[0]

    def test_soundscape_chunks_vs_speakers(
        self, embedder, speaker_a_audios, speaker_b_audios, soundscape_audio
    ):
        sc_chunks = embedder.predict([soundscape_audio])[0].chunk_embeddings
        a_emb = embedder.predict([speaker_a_audios[0]])[0].embedding
        b_emb = embedder.predict([speaker_b_audios[0]])[0].embedding

        a_sims = [self._cosine(c, a_emb) for c in sc_chunks]
        b_sims = [self._cosine(c, b_emb) for c in sc_chunks]

        assert np.mean(a_sims) < 0.4
        assert np.mean(b_sims) < 0.4


class TestRejection:
    def test_predict_before_start_raises(self, speaker_a_audios):
        emb = AudioEmbedder(model_path=MODEL_PATH, processor_factory=AudioProcessorFactory(
            enable_resample=False, enable_high_pass=False,
            enable_noise_reduce=False, enable_vad=False, enable_rms_normalize=False,
        ))
        with pytest.raises(RuntimeError, match="not ready"):
            emb.predict([speaker_a_audios[0]])

    def test_predict_after_stop_raises(self, speaker_a_audios):
        emb = AudioEmbedder(model_path=MODEL_PATH, processor_factory=AudioProcessorFactory(
            enable_resample=False, enable_high_pass=False,
            enable_noise_reduce=False, enable_vad=False, enable_rms_normalize=False,
        ))
        emb.start()
        emb.stop()
        with pytest.raises(RuntimeError, match="not ready"):
            emb.predict([speaker_a_audios[0]])


class TestEmbedderPerformance:
    """Performance tests asserting embedding quality for speaker similarity."""

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

    def test_same_speaker_higher_similarity(
        self, embedder: AudioEmbedder, speaker_a_audios: list
    ) -> None:
        """Two clips from the same speaker should have cosine similarity > 0.5."""
        results = embedder.predict(speaker_a_audios[:2])
        assert len(results) >= 2
        sim = self._cosine(results[0].embedding, results[1].embedding)
        assert sim > 0.5, (
            f"Same-speaker cosine similarity {sim:.4f} should be > 0.5"
        )

    def test_different_speaker_lower_similarity(
        self,
        embedder: AudioEmbedder,
        speaker_a_audios: list,
        speaker_b_audios: list,
    ) -> None:
        """Cross-speaker similarity should be lower than same-speaker similarity."""
        a_results = embedder.predict(speaker_a_audios[:2])
        b_results = embedder.predict(speaker_b_audios[:1])

        same_sim = self._cosine(a_results[0].embedding, a_results[1].embedding)
        diff_sim = self._cosine(a_results[0].embedding, b_results[0].embedding)

        assert diff_sim < same_sim, (
            f"Cross-speaker similarity ({diff_sim:.4f}) should be less than "
            f"same-speaker similarity ({same_sim:.4f})"
        )


class TestPreprocessRejection:
    def test_vad_rejects_silence(self):
        vad = VoiceActivityFilter(min_duration_sec=0.5)
        vad.start()
        silence = Audio(
            waveform=np.zeros(16000, dtype=np.float32),
            sample_rate=16000,
        )
        with pytest.raises(PreprocessRejected):
            vad.process(silence)
        vad.stop()

    def test_vad_rejects_too_short(self):
        vad = VoiceActivityFilter(min_duration_sec=10.0)
        vad.start()
        short = Audio(
            waveform=np.random.randn(8000).astype(np.float32),
            sample_rate=16000,
        )
        with pytest.raises(PreprocessRejected):
            vad.process(short)
        vad.stop()
