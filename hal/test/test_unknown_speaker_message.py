"""An unknown speaker's turn must not carry an order to ask for their name.

The message HAL builds here is glued onto the transcript the model answers, so
anything imperative in it competes with the silence rules in the persona — and a
nearer, more specific instruction wins. That is how one word of somebody else's
conversation ("program.") produced a spoken "I don't think we've met, what's
your name?".
"""

from unittest import mock

from hal.drivers.voice._internal.speaker_decorate import SpeakerDecorator


def _decorator():
    with mock.patch.object(SpeakerDecorator, "_init_speaker", return_value=None), \
         mock.patch.object(SpeakerDecorator, "_init_speech_emotion", return_value=None):
        return SpeakerDecorator(wake_words=["lamp"], nudge_cooldown_s=0)


def test_a_short_fragment_carries_no_instruction_to_speak():
    msg = _decorator()._format_unknown_speaker_message(
        "program.", "/tmp/v.wav", duration_s=0.4, voiceprint_hash="voice_460"
    )

    assert "ask" not in msg.lower()
    # The path and the tag still ride along, so a later real turn can enrol.
    assert "/tmp/v.wav" in msg
    assert "voice_460" in msg


def test_a_full_utterance_still_gets_the_enroll_nudge():
    # Ten words and over two seconds — the gate for the strong nudge, unchanged.
    msg = _decorator()._format_unknown_speaker_message(
        "hello there I am a brand new person in this house",
        "/tmp/v.wav",
        duration_s=4.0,
        voiceprint_hash="voice_461",
    )

    assert "ask user's name" in msg


def test_a_speaker_asked_recently_is_not_asked_again():
    d = _decorator()
    d._nudge_cooldown_s = 300
    long_turn = "hello there I am a brand new person in this house"
    first = d._format_unknown_speaker_message(
        long_turn, "/tmp/a.wav", duration_s=4.0, voiceprint_hash="voice_462"
    )
    second = d._format_unknown_speaker_message(
        long_turn, "/tmp/b.wav", duration_s=4.0, voiceprint_hash="voice_462"
    )

    assert "ask user's name" in first
    assert "ask" not in second.lower()
