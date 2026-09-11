"""Regression coverage for song-intent planning leaking into spoken replies."""

import pytest

from hal.drivers.voice._internal.cot_leak_filter import CoTLeakFilter, clean_transcript


@pytest.mark.parametrize("language", ["English", ""])
def test_song_request_keeps_confirmation_without_internal_planning(language):
    transcript = (
        'They named a song: "Eternal Flame" — likely the Bangles song. '
        "Play it.Speaker is unknown — no person to attribute. "
        "Playing Eternal Flame by The Bangles. "
        "Playing Eternal Flame — one of the all-time greats! "
        "Now that's what I call a favorite."
    )
    expected = (
        "Playing Eternal Flame by The Bangles. "
        "Playing Eternal Flame — one of the all-time greats! "
        "Now that's what I call a favorite."
    )
    assert clean_transcript(transcript, language) == expected


@pytest.mark.parametrize("language", ["English", ""])
@pytest.mark.parametrize(
    "planning",
    [
        'They named the song: "Eternal Flame" — likely the Bangles song.',
        "Speaker is unknown — no person to attribute.",
        "Speaker identity is unknown — no person to attribute.",
    ],
)
def test_song_planning_markers_work_independently(language, planning):
    speech_filter = CoTLeakFilter(language)
    assert speech_filter.filter_text(planning) == ""
    assert speech_filter.filter_text("Playing Eternal Flame by The Bangles.") == (
        "Playing Eternal Flame by The Bangles."
    )


@pytest.mark.parametrize(
    "reply",
    [
        "They named a song after their hometown.",
        "They named the song Eternal Flame.",
        "In the interview they named a song: Eternal Flame.",
        "Your speaker is connected over Bluetooth.",
        "Your speaker is unknown to this Bluetooth adapter.",
        "The speaker identity is unknown to the conference organizers.",
    ],
)
def test_normal_song_and_physical_speaker_discussion_is_preserved(reply):
    assert clean_transcript(reply, "English") == reply
