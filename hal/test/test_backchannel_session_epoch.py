"""Queued backchannel audio must not outlive the STT session that requested it."""

from hal.drivers.voice.backchannel import Backchannel


class _UnexpectedTTS:
    """Fails if a stale cue reaches the TTS backend."""

    @property
    def _backend(self):
        raise AssertionError("stale backchannel cue reached TTS")


def test_ended_session_invalidates_its_queued_backchannel_cue():
    """A delayed cue cannot be played after its source capture closes."""
    backchannel = Backchannel(None)

    backchannel.begin_session()
    source_epoch = backchannel._session_epoch
    assert backchannel._session_is_current(source_epoch)

    backchannel.reset()

    assert not backchannel._session_is_current(source_epoch)


def test_stale_cue_is_cancelled_before_it_touches_tts():
    """A queued cue waiting behind regular TTS must be a no-op after reset."""
    backchannel = Backchannel(_UnexpectedTTS())

    backchannel.begin_session()
    source_epoch = backchannel._session_epoch
    backchannel.reset()

    backchannel._play("Right", source_epoch)


def test_new_session_does_not_revive_a_previous_cue():
    """A later capture gets a distinct token, even if it starts immediately."""
    backchannel = Backchannel(None)

    backchannel.begin_session()
    old_epoch = backchannel._session_epoch
    backchannel.reset()
    backchannel.begin_session()
    new_epoch = backchannel._session_epoch

    assert new_epoch != old_epoch
    assert not backchannel._session_is_current(old_epoch)
    assert backchannel._session_is_current(new_epoch)
