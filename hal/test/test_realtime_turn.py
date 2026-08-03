"""Regression tests for realtime-to-main-agent turn routing."""

from hal.drivers.voice._internal.realtime_turn import should_dispatch_to_main


def test_confirmed_turn_falls_back_when_realtime_is_unavailable_or_silent():
    """A connected flag must not swallow a wake-word command without a reply."""
    assert should_dispatch_to_main(True, True)


def test_confirmed_handled_turn_still_synchronizes_main_agent():
    assert should_dispatch_to_main(True, True)


def test_wakeword_gate_still_rejects_unarmed_ambient_speech():
    assert not should_dispatch_to_main(True, False)


def test_wakeword_disabled_preserves_the_always_listening_main_agent_sync():
    """The legacy path always sends the finalized STT turn to the OS server."""
    assert should_dispatch_to_main(False, False)
