"""Focused tests for publishing the look frame to the Flow Monitor.

The important guarantees: the frame path reaches the event (the monitor derives
the thumbnail URL from it), and a publish failure never propagates — a missing
thumbnail must not cost the user an answer.
"""

from unittest import mock

from hal.realtime import look_monitor


def _resp(status=200):
    r = mock.Mock()
    r.status_code = status
    return r


def test_publishes_the_frame_path_in_the_event():
    with mock.patch.object(look_monitor.requests, "post", return_value=_resp()) as post:
        ok = look_monitor.publish_look_frame(
            "/root/.openclaw/media/hal-snapshots/look_1712345678901.jpg"
        )
    assert ok is True
    payload = post.call_args.kwargs["json"]
    assert payload["type"] == "look.capture"
    # The monitor's URL builder matches the raw path out of the message.
    assert "/root/.openclaw/media/hal-snapshots/look_1712345678901.jpg" in payload["message"]


def test_no_path_does_not_post():
    with mock.patch.object(look_monitor.requests, "post") as post:
        assert look_monitor.publish_look_frame(None) is False
        assert look_monitor.publish_look_frame("") is False
    assert not post.called


def test_http_error_is_reported_not_raised():
    with mock.patch.object(look_monitor.requests, "post", return_value=_resp(500)):
        assert look_monitor.publish_look_frame("/root/.openclaw/media/hal-snapshots/look_1.jpg") is False


def test_transport_failure_never_raises():
    # os-server down mid-turn must not take the answer down with it.
    with mock.patch.object(look_monitor.requests, "post", side_effect=OSError("connection refused")):
        assert look_monitor.publish_look_frame("/root/.openclaw/media/hal-snapshots/look_1.jpg") is False


def test_post_is_time_bounded():
    with mock.patch.object(look_monitor.requests, "post", return_value=_resp()) as post:
        look_monitor.publish_look_frame("/root/.openclaw/media/hal-snapshots/look_1.jpg")
    assert post.call_args.kwargs["timeout"] <= 2.0
