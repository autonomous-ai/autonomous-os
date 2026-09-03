"""A failed summarization must say what the server actually sent."""

import logging
from unittest import mock

from hal.realtime.summarizer import RealtimeSummarizer


def test_undecodable_body_is_reported_as_bytes(caplog):
    exc = UnicodeDecodeError("utf-8", b"\x1f\x8b\x08\x00\xc4\x01", 4, 5, "invalid continuation byte")
    with caplog.at_level(logging.WARNING):
        RealtimeSummarizer._log_failure_evidence(exc)
    logged = caplog.text
    assert "undecodable response body" in logged
    assert "1f 8b 08 00 c4" in logged, logged


def test_http_error_reports_status_and_headers(caplog):
    response = mock.Mock()
    response.status_code = 502
    response.headers = {"content-type": "text/html", "content-encoding": "br"}
    response.content = b"<html>Bad Gateway</html>"
    response.request.url = "https://example.invalid/v1/messages"
    exc = RuntimeError("boom")
    exc.response = response
    with caplog.at_level(logging.WARNING):
        RealtimeSummarizer._log_failure_evidence(exc)
    logged = caplog.text
    assert "HTTP 502" in logged
    assert "text/html" in logged and "'br'" in logged
    assert "Bad Gateway" in logged


# Diagnostics must never replace the real failure with one of their own.
def test_broken_response_object_does_not_raise(caplog):
    exc = RuntimeError("boom")
    exc.response = object()  # no status_code, no headers, no content
    with caplog.at_level(logging.WARNING):
        RealtimeSummarizer._log_failure_evidence(exc)


def test_plain_exception_logs_nothing_extra(caplog):
    with caplog.at_level(logging.WARNING):
        RealtimeSummarizer._log_failure_evidence(ValueError("no response attached"))
    assert caplog.text == ""


# --- the summary itself ----------------------------------------------------


class _FakeStream:
    def __init__(self, parts):
        self.text_stream = iter(parts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ready_summarizer(stream_parts):
    s = RealtimeSummarizer.__new__(RealtimeSummarizer)
    s._system_prompt = "sys"
    s._model = "m"
    s._base_url = "https://example.invalid"
    client = mock.Mock()
    client.messages.stream.return_value = _FakeStream(stream_parts)
    s._get_client = lambda: client
    return s, client


# The gateway's non-streaming /v1/messages returns an undecodable body, so the
# summarizer must never take that path.
def test_summarize_streams_and_never_calls_the_non_streaming_endpoint():
    s, client = _ready_summarizer(["Long ", "asked ", "about lamps."])
    assert s.summarize(["user: hi", "lamp: hello"]) == "Long asked about lamps."
    client.messages.stream.assert_called_once()
    client.messages.create.assert_not_called()


def test_empty_entries_short_circuit_without_a_request():
    s, client = _ready_summarizer([])
    assert s.summarize(["", "   "]) == ""
    client.messages.stream.assert_not_called()


def test_a_stream_failure_returns_empty_instead_of_raising(caplog):
    s = RealtimeSummarizer.__new__(RealtimeSummarizer)
    s._system_prompt, s._model, s._base_url = "sys", "m", "u"
    client = mock.Mock()
    client.messages.stream.side_effect = RuntimeError("gateway down")
    s._get_client = lambda: client
    assert s.summarize(["user: hi"]) == ""
    assert "Summarization failed" in caplog.text
