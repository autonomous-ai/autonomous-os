"""Confirming a wake word when STT rewrites the name in its final transcript."""

from hal.drivers.voice._internal.speaker_decorate import SpeakerDecorator


def _decorator():
    d = SpeakerDecorator.__new__(SpeakerDecorator)
    import threading

    d._wake_words_lock = threading.Lock()
    d._wake_words = ["hello lamp", "hey lamp", "hey rachel"]
    return d


def test_a_one_letter_slip_still_confirms():
    """The device-observed case: partial 'hello lamp' → final 'hello lamb'."""
    d = _decorator()
    assert not d.starts_with_wake_word("Hello, lamb. Can you hear me?")
    assert d.matches_wake_word_loosely("Hello, lamb. Can you hear me?")


def test_insertions_and_deletions_count_as_one_slip():
    d = _decorator()
    assert d.matches_wake_word_loosely("Hey rachael, what time is it?")
    assert d.matches_wake_word_loosely("Hey rachl, what time is it?")


def test_two_letters_off_is_a_different_word():
    d = _decorator()
    assert not d.matches_wake_word_loosely("Hello lance, can you hear me?")


def test_the_prefix_must_still_match_exactly():
    """Only the name may slip; loosening 'hello' would match ordinary speech."""
    d = _decorator()
    assert not d.matches_wake_word_loosely("Hellu lamp, can you hear me?")


def test_mid_sentence_is_still_rejected():
    d = _decorator()
    assert not d.matches_wake_word_loosely("I think this lamb is nice")


def test_exact_match_is_unaffected():
    d = _decorator()
    assert d.starts_with_wake_word("Hello lamp, can you hear me?")
    assert d.matches_wake_word_loosely("Hello lamp, can you hear me?")
