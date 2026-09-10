"""Admission gates on the speaker auto-extend path.

Pure gate math — no audio, no embedding server, no disk. Each test drives
_maybe_extend_user directly and asserts only whether a sample was written,
by stubbing the one method that touches disk.
"""

import numpy as np
import pytest

from hal.drivers.voice.speaker_recognizer.speaker_recognizer import SpeakerRecognizer


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
        lambda norm, wav, emb: written.append(norm) or None,
    )
    return written


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
        "leo", np.ones(8, dtype=np.float32), b"RIFFfake", **kwargs,
    )


def test_a_clean_unanimous_turn_is_admitted(recognizer, spy):
    _extend(recognizer)
    assert spy == ["leo"], "a clean turn must still extend"


def test_a_split_vote_is_rejected(recognizer, spy):
    # 4 of 7 chunks voted for somebody else -> multi-speaker audio (A1).
    _extend(recognizer, chunk_votes=4, num_chunks=7)
    assert spy == [], "a split vote must never reach the bank"


def test_a_weak_chunk_is_rejected(recognizer, spy):
    # Unanimous, average passes, but one chunk is barely above noise (A2).
    _extend(recognizer, min_chunk_cos=0.2)
    assert spy == [], "an unsure chunk must reject the whole turn"


def test_the_single_enrolled_speaker_hole_is_closed(recognizer, spy):
    # One enrolled speaker means one column, so every chunk votes for them by
    # default even at cos 0.2. Unanimous and meaningless -- A2 is what catches
    # this, and it is the common real case (the other person is a guest).
    _extend(recognizer, chunk_votes=7, num_chunks=7, min_chunk_cos=0.2)
    assert spy == [], "unanimity alone must not admit a weak turn"


def test_a_short_single_chunk_turn_is_unchanged(recognizer, spy):
    # <=10s of post-VAD speech is ONE chunk: A1 is 1-of-1 and A2 reduces to
    # the match threshold that is_match already enforced. Behaviour here must
    # be bit-identical to pre-fix.
    _extend(recognizer, chunk_votes=1, num_chunks=1, min_chunk_cos=0.55)
    assert spy == ["leo"], "short turns must be unaffected by the chunk gates"
