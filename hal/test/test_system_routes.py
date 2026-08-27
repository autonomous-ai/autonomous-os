"""System HTTP actions dispatch the explicit action flows, not gestures."""

from unittest import mock

from hal.routes import system


def test_reboot_starts_reboot_action_in_background():
    thread = mock.Mock()
    with mock.patch.object(system.threading, "Thread", return_value=thread) as new_thread:
        assert system.reboot_os() == {"status": "rebooting"}

    new_thread.assert_called_once_with(
        target=system.reboot_action,
        kwargs={"source": "system API"},
        daemon=True,
        name="system-api-reboot",
    )
    thread.start.assert_called_once_with()


def test_shutdown_starts_shutdown_action_in_background():
    thread = mock.Mock()
    with mock.patch.object(system.threading, "Thread", return_value=thread) as new_thread:
        assert system.shutdown_os() == {"status": "shutting down"}

    new_thread.assert_called_once_with(
        target=system.shutdown_action,
        kwargs={"source": "system API"},
        daemon=True,
        name="system-api-shutdown",
    )
    thread.start.assert_called_once_with()
