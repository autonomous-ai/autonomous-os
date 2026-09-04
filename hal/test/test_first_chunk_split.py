"""Time-to-first-audio: the opening of a turn is cut at a clause boundary."""

from hal import config as hal_config
from hal.drivers.voice._internal.realtime_turn import split_first_chunk


def test_waits_while_the_opening_is_still_a_runt():
    assert split_first_chunk("Sure,") == ("", "Sure,")


def test_speaks_the_head_at_the_last_clause_boundary():
    head, rest = split_first_chunk("Well, that depends on where you are, but I can check")
    assert head == "Well, that depends on where you are,"
    assert rest.strip() == "but I can check"


def test_falls_back_to_a_word_break_past_the_cap():
    buf = " ".join(["word"] * 30)
    head, rest = split_first_chunk(buf)
    assert head and rest
    assert len(head) <= hal_config.REALTIME_FIRST_CHUNK_MAX_CHARS
    # Nothing is lost or duplicated; the two halves are spoken in order.
    assert (head + " " + rest).split() == buf.split()


def test_short_comma_less_opening_keeps_buffering():
    assert split_first_chunk("Hello there") == ("", "Hello there")


def test_zero_disables_the_early_cut(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_FIRST_CHUNK_MAX_CHARS", 0)
    text = "Well, that depends on where you are, but I can check"
    assert split_first_chunk(text) == ("", text)
