"""Every turn logs where it went and why — not just the delegated ones."""

import logging

import pytest

from hal.drivers.voice._internal.realtime_turn import (
    ROUTE_DELEGATED,
    ROUTE_HANDLED,
    ROUTE_NO_OUTPUT,
    ROUTE_NOISE_DROPPED,
    RealtimeTurnResult,
)
from hal.drivers.voice._internal.turn_dispatch import dispatch_turn


class _Decorator:
    def classify_wake_word(self, combined):
        return combined, "voice"

    def identify_and_decorate(self, final_text, audio_buffer):
        return final_text, "leo", "Leo"

    def submit_speech_emotion_from_session(self, buf, user=None):
        pass


class _Sender:
    def __init__(self):
        self.sent = []

    def send(self, msg, event_type="", skip_echo=False, image_b64=""):
        self.sent.append((msg, event_type))


def _dispatch(rt, combined, caplog, event_type_override=None):
    with caplog.at_level(logging.INFO, logger="hal.voice"):
        dispatch_turn(
            _Decorator(),
            _Sender(),
            combined,
            [],
            [],
            rt,
            event_type_override=event_type_override,
        )
    return [r.getMessage() for r in caplog.records if "[turn] route=" in r.getMessage()]


def test_a_turn_the_main_agent_answers_names_its_route(caplog):
    # The case that was invisible: realtime committed but nothing came back, so
    # the main agent answers — with no "delegated" line anywhere in the journal.
    lines = _dispatch(
        RealtimeTurnResult(route=ROUTE_NO_OUTPUT), "research my email", caplog
    )
    assert len(lines) == 1
    assert ROUTE_NO_OUTPUT in lines[0]
    assert "main agent" in lines[0]


def test_delegated_turn_is_labelled_as_such(caplog):
    lines = _dispatch(
        RealtimeTurnResult(delegated=True, route=ROUTE_DELEGATED), "turn on the light", caplog
    )
    assert ROUTE_DELEGATED in lines[0]
    assert "main agent" in lines[0]


def test_handled_turn_does_not_claim_the_main_agent_answers(caplog):
    lines = _dispatch(
        RealtimeTurnResult(handled=True, transcript="sure", route=ROUTE_HANDLED),
        "hello there",
        caplog,
    )
    assert ROUTE_HANDLED in lines[0]
    assert "main agent" not in lines[0].split("→")[1].split("(")[0]


def test_dropped_turn_reports_that_it_reached_nobody(caplog):
    lines = _dispatch(RealtimeTurnResult(route=ROUTE_NOISE_DROPPED), "", caplog)
    assert ROUTE_NOISE_DROPPED in lines[0]
    assert "nowhere" in lines[0]


def test_route_defaults_so_an_old_style_result_still_logs(caplog):
    lines = _dispatch(RealtimeTurnResult(), "hello", caplog)
    assert len(lines) == 1


@pytest.mark.parametrize("override", ["voice_followup", None])
def test_event_type_travels_with_the_route(caplog, override):
    lines = _dispatch(
        RealtimeTurnResult(route=ROUTE_NO_OUTPUT),
        "research my email",
        caplog,
        event_type_override=override,
    )
    assert f"event={override or 'voice'}" in lines[0]
