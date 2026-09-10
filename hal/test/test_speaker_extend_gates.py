"""Admission gates on the speaker auto-extend path.

Pure gate math — no audio, no embedding server, no disk. Each test drives
_maybe_extend_user directly and asserts only whether a sample was written,
by stubbing the one method that touches disk.
"""

import numpy as np
import pytest

from hal.drivers.voice.speaker_recognizer.speaker_recognizer import (
    SpeakerRecognizer,
    _runner_up_mean,
)


@pytest.fixture
def recognizer(tmp_path):
    """A recognizer with a temp user dir and the default 0.5 match bar."""
    return SpeakerRecognizer(
        api_url="http://unused", api_key="k", users_dir=tmp_path,
        match_threshold=0.5,
    )


@pytest.fixture
def spy(recognizer, monkeypatch):
    """Record extend attempts instead of writing to disk.

    Returns a list that stays empty when a gate rejected the turn.
    """
    written = []
    monkeypatch.setattr(
        recognizer, "_write_extended_sample",
        lambda norm, wav, emb, **kw: written.append(norm) or None,
    )
    return written


@pytest.fixture
def embed(recognizer, monkeypatch):
    """Stub the embedding API and record how it was called.

    Returns the list of (payload, use_sliding_window) calls, so a test can
    assert BOTH that the stored vector came from a single-shot call and that a
    rejected turn made no call at all.
    """
    calls = []

    def _fake(audios_b64, *, use_sliding_window=False):
        calls.append((audios_b64, use_sliding_window))
        return np.ones(8, dtype=np.float32) / np.sqrt(8.0)

    monkeypatch.setattr(recognizer, "_call_embedding_api", _fake)
    return calls


def _extend(recognizer, **over):
    """Call _maybe_extend_user with all gates wide open unless overridden."""
    kwargs = dict(
        existing_rows=None,      # no redundancy check
        duration_s=10.0,         # clears the 2.0s floor
        margin=1.0,              # clears the 0.05 margin floor
        chunk_votes=7,
        num_chunks=7,            # unanimous
        min_chunk_cos=0.9,       # every chunk well above the 0.5 bar
    )
    kwargs.update(over)
    recognizer._maybe_extend_user(
        "leo", ["Y2xlYW5lZC13YXY="], b"RIFFfake", **kwargs,
    )


def test_a_clean_unanimous_turn_is_admitted(recognizer, spy, embed):
    _extend(recognizer)
    assert spy == ["leo"], "a clean turn must still extend"


def test_a_split_vote_is_rejected(recognizer, spy, embed):
    # 4 of 7 chunks voted for somebody else -> multi-speaker audio (A1).
    _extend(recognizer, chunk_votes=4, num_chunks=7)
    assert spy == [], "a split vote must never reach the bank"


def test_a_weak_chunk_is_rejected(recognizer, spy, embed):
    # Unanimous, average passes, but one chunk is barely above noise (A2).
    _extend(recognizer, min_chunk_cos=0.2)
    assert spy == [], "an unsure chunk must reject the whole turn"


def test_the_single_enrolled_speaker_hole_is_closed(recognizer, spy, embed):
    # One enrolled speaker means one column, so every chunk votes for them by
    # default even at cos 0.2. Unanimous and meaningless -- A2 is what catches
    # this, and it is the common real case (the other person is a guest).
    _extend(recognizer, chunk_votes=7, num_chunks=7, min_chunk_cos=0.2)
    assert spy == [], "unanimity alone must not admit a weak turn"


def test_a_short_single_chunk_turn_is_unchanged(recognizer, spy, embed):
    # <=10s of post-VAD speech is ONE chunk: A1 is 1-of-1 and A2 reduces to
    # the match threshold that is_match already enforced. Behaviour here must
    # be bit-identical to pre-fix.
    _extend(recognizer, chunk_votes=1, num_chunks=1, min_chunk_cos=0.55)
    assert spy == ["leo"], "short turns must be unaffected by the chunk gates"


def test_a_unanimous_near_tie_has_a_real_runner_up():
    # Three enrolled users whose per-chunk scores sit 0.01 apart. Every chunk
    # votes leo, so the OLD margin -- built from vote winners only -- was `inf`
    # and the gate did nothing. That is exactly the near-tie its docstring
    # promises to block.
    names = ["leo", "mia", "sam"]
    confs = np.array([[0.55, 0.54, 0.53],
                      [0.56, 0.55, 0.54],
                      [0.57, 0.56, 0.55]])
    best_conf = float(confs[:, 0].mean())
    margin = best_conf - _runner_up_mean(confs, names, "leo")
    assert margin == pytest.approx(0.01, abs=1e-6), (
        f"a 0.01 near-tie must surface as a 0.01 margin, got {margin}"
    )
    assert margin < 0.05, "and must therefore fail the 0.05 margin bar"


def test_a_single_enrolled_user_has_no_runner_up():
    # Nobody to compare against, so `inf` is the CORRECT margin -- there is
    # genuinely no relative signal available. This must keep working, and is
    # why an absolute anchor floor (Task 4) is the only gate that helps a
    # single-user device.
    confs = np.array([[0.80], [0.82], [0.79]])
    assert _runner_up_mean(confs, ["leo"], "leo") == float("-inf")


def test_the_runner_up_ignores_the_winners_own_column():
    confs = np.array([[0.90, 0.10], [0.90, 0.10]])
    assert _runner_up_mean(confs, ["leo", "mia"], "leo") == pytest.approx(0.10)


def test_the_runner_up_is_the_strongest_loser():
    # Three columns: the runner-up is the best of the two non-winners, not the
    # average of them and not the last one.
    confs = np.array([[0.90, 0.20, 0.60], [0.90, 0.20, 0.60]])
    assert _runner_up_mean(confs, ["leo", "mia", "sam"], "leo") == pytest.approx(0.60)


def test_the_reported_duration_is_the_cleaned_length_not_the_raw_length(
    recognizer, monkeypatch,
):
    # A 28s file holding 1.5s of speech: exactly the shape a 30s mic session
    # produces. VAD trims it to 1.5s, so the duration handed to the extend
    # gate must be 1.5 -- not the 28 the raw WAV reports.
    #
    # Unlike the gate tests above this one exercises the real preprocessing
    # entry point, so it needs the audio_processors stack (scipy). That is
    # present on a body and in CI but not on every dev host, hence the skip.
    pytest.importorskip(
        "scipy", reason="audio_processors requires scipy; run this on-device"
    )
    from hal.drivers.voice.speaker_recognizer import speaker_recognizer as sr_mod
    from hal.drivers.voice.speaker_recognizer.audio_processors.base import Audio

    sr = 16000
    raw = np.zeros(28 * sr, dtype=np.float32)
    raw[: int(1.5 * sr)] = 0.2

    class _TrimToSpeech:
        """Stand-in for the VAD chain: returns only the voiced part."""

        def process(self, audio):
            return Audio(
                waveform=audio.waveform[: int(1.5 * sr)], sample_rate=sr
            )

    monkeypatch.setattr(sr_mod, "_get_audio_processor", lambda: _TrimToSpeech())

    wav = sr_mod._float32_waveform_to_wav_bytes(raw)
    assert sr_mod._wav_duration_s(wav) == pytest.approx(28.0, abs=0.05), (
        "the raw WAV really is 28s -- this is what the gate used to see"
    )

    payload, cleaned_duration_s = recognizer._prepare_wav_for_embedding(wav)
    assert isinstance(payload, list) and payload, "payload shape must not change"
    assert cleaned_duration_s == pytest.approx(1.5, abs=0.05), (
        f"duration must be measured after VAD, got {cleaned_duration_s}"
    )
    assert cleaned_duration_s < 2.0, (
        "and must therefore fail the 2.0s extend floor that 28s cleared"
    )


def test_a_match_carried_only_by_the_extended_tier_cannot_extend(recognizer, spy, embed):
    # The whole point: a match the extended bank carried is evidence about a
    # previous guess, not about the person. Letting it add a row lets one
    # mistake breed more.
    _extend(recognizer, anchor_cos=0.31)
    assert spy == [], "an extended-carried match must not grow the bank"


def test_a_match_carried_by_the_anchors_still_extends(recognizer, spy, embed):
    _extend(recognizer, anchor_cos=0.72)
    assert spy == ["leo"], "an anchor-carried match must still extend"


def test_a_user_with_no_anchor_rows_does_not_extend(recognizer, spy, embed):
    # Legacy profile with no readable anchor tier: -inf, so it cannot vouch
    # for itself. Better to stop growing than to grow unanchored.
    _extend(recognizer, anchor_cos=float("-inf"))
    assert spy == [], "no anchor evidence means no extend"


def test_an_admitted_sample_records_why_it_was_admitted(recognizer, tmp_path):
    path = recognizer._write_extended_sample(
        "leo", b"RIFFfake", np.ones(8, dtype=np.float32),
        provenance={"min_chunk_cos": 0.61, "anchor_cos": 0.72},
    )
    assert path is not None
    import json
    meta = json.loads(path.with_suffix(".json").read_text())
    assert meta["min_chunk_cos"] == 0.61
    assert meta["anchor_cos"] == 0.72


def test_deleting_a_sample_removes_its_provenance_too(recognizer):
    path = recognizer._write_extended_sample(
        "leo", b"RIFFfake", np.ones(8, dtype=np.float32),
        provenance={"anchor_cos": 0.72},
    )
    assert path is not None and path.with_suffix(".json").is_file()
    recognizer._delete_sample(path)
    assert not path.is_file(), "the wav must go"
    assert not path.with_suffix(".json").is_file(), "and so must its provenance"


def test_the_stored_vector_comes_from_a_single_shot_call(recognizer, spy, embed):
    # The whole point: an extended sample must be embedded the way enroll,
    # backfill and migration embed -- whole utterance, no windowing, no mean.
    _extend(recognizer)
    assert spy == ["leo"], "the turn should have been admitted"
    assert len(embed) == 1, f"expected exactly one embed call, got {len(embed)}"
    payload, sliding = embed[0]
    assert payload == ["Y2xlYW5lZC13YXY="], "must embed the cleaned WAV payload"
    assert sliding is False, (
        "use_sliding_window must be False -- a True call returns per-chunk "
        "vectors, which is the mean-of-chunks behaviour this replaces"
    )


def test_a_rejected_turn_costs_no_embed_call(recognizer, spy, embed):
    # The extra network call must sit AFTER the five cheap gates, so a turn
    # that was never going to be stored does not pay for one.
    _extend(recognizer, chunk_votes=4, num_chunks=7)   # fails gate 1
    assert spy == [], "gate 1 should have rejected"
    assert embed == [], "a rejected turn must not call the embedding API"


def test_the_diversity_gate_tests_the_vector_being_stored(recognizer, spy, embed):
    # The stub returns the unit vector ones/sqrt(8); an identical existing row
    # gives cosine 1.0, well above the 0.7 diversity bar -> redundant.
    existing = (np.ones((1, 8), dtype=np.float32) / np.sqrt(8.0))
    _extend(recognizer, existing_rows=existing)
    assert spy == [], "an identical stored row must read as redundant"
    assert len(embed) == 1, "but only after paying for the embedding"


def test_an_embedding_failure_skips_the_extend_without_raising(
    recognizer, spy, monkeypatch,
):
    # Bank maintenance must never break a turn: the identity decision is
    # already made and the reply depends on it.
    from hal.drivers.voice.speaker_recognizer.speaker_recognizer import (
        EmbeddingAPIUnavailableError,
    )

    def _boom(audios_b64, *, use_sliding_window=False):
        raise EmbeddingAPIUnavailableError("embedding server down")

    monkeypatch.setattr(recognizer, "_call_embedding_api", _boom)
    _extend(recognizer)          # must not raise
    assert spy == [], "a failed embed must not write a sample"


def test_the_provenance_records_the_embedding_mode(recognizer, embed, tmp_path):
    # Old samples hold a mean and carry no mode; new ones say so explicitly, so
    # a later audit can tell the two populations apart on disk.
    written = {}
    real = recognizer._write_extended_sample

    def _capture(norm, wav, emb, provenance=None):
        written.update(provenance or {})
        return real(norm, wav, emb, provenance=provenance)

    recognizer._write_extended_sample = _capture
    _extend(recognizer)
    assert written.get("embedding_mode") == "single_shot"
