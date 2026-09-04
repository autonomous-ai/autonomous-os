"""Who the device believes it is being spoken to — the listening-cue gate."""

from hal.drivers.voice._internal.wakeword_focus import is_addressed


def test_no_wake_word_configured_means_every_utterance_is_ours():
    assert is_addressed(False, False, False, False)


def test_a_heard_wake_word_is_enough():
    assert is_addressed(True, True, False, False)


def test_focus_latched_at_session_start_is_enough():
    assert is_addressed(True, False, True, False)


def test_gaze_opening_focus_mid_sentence_lights_the_cue():
    """The latch was False (no face evidence yet) and gaze confirmed late."""
    assert is_addressed(True, False, False, True)


def test_ambient_speech_with_no_evidence_stays_unaddressed():
    assert not is_addressed(True, False, False, False)


def test_a_window_expiring_mid_sentence_cannot_retract_the_turn():
    """Latched at session start, expired by now — still ours."""
    assert is_addressed(True, False, True, False)
