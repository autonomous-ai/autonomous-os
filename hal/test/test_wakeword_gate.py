"""Unit tests for the strict Deepgram interim wake-word matcher."""

import threading

from hal.drivers.voice._internal.speaker_decorate import (
    SpeakerDecorator,
    merge_stt_hypothesis,
    merge_wake_words,
)


def _decorator(words: list[str]) -> SpeakerDecorator:
    decorator = object.__new__(SpeakerDecorator)
    decorator._wake_words = words
    decorator._wake_words_lock = threading.Lock()
    return decorator


def test_wake_word_match_is_case_and_punctuation_insensitive():
    decorator = _decorator(["hello luna", "hey luna", "hi luna", "alo luna", "okay luna", "ok luna", "wake up luna"])

    assert decorator.starts_with_wake_word("Hey Luna, thời tiết hôm nay?")
    assert decorator.starts_with_wake_word("Hello Luna! kể chuyện đi")
    assert decorator.starts_with_wake_word("Alo Luna ơi, kể chuyện đi")
    assert decorator.starts_with_wake_word("wake up Luna xem giúp mình")


def test_wake_word_match_requires_a_prefix_and_word_boundary():
    decorator = _decorator(["hey luna", "wake up luna"])

    assert not decorator.starts_with_wake_word("Mình vừa gặp Luna ngoài đường")
    assert not decorator.starts_with_wake_word("lunar calendar")
    assert not decorator.starts_with_wake_word("luna, nghe mình nói này")
    assert decorator.starts_with_wake_word("wake up luna, nghe mình nói này")


def test_partial_hypothesis_reassembles_cumulative_and_delta_updates():
    decorator = _decorator(["hello luna"])

    cumulative = merge_stt_hypothesis("hello", "hello luna")
    delta = merge_stt_hypothesis("hello", "luna")
    overlapping = merge_stt_hypothesis("hello luna", "luna what time is it")

    assert decorator.starts_with_wake_word(cumulative)
    assert decorator.starts_with_wake_word(delta)
    assert overlapping == "hello luna what time is it"


def test_device_type_alias_is_retained_alongside_agent_name():
    words = merge_wake_words(
        ["hello autonomous", "hey autonomous", "hi lamp", "wake up lamp"],
        ["hey luna", "wake up luna"],
    )

    assert words == [
        "hello autonomous",
        "hey autonomous",
        "hi lamp",
        "wake up lamp",
        "hey luna",
        "wake up luna",
    ]
