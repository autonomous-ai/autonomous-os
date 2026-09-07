"""Voice KPI measurement tests.

Every test uses a mock transport — no event ever leaves the process, and the
production analytics endpoint is never contacted.
"""

import time

import pytest

from hal.tracking import client, voice_kpi


@pytest.fixture
def sent(monkeypatch):
    """Collect reported events instead of queueing them for the network."""
    events = []

    def fake_report(event_name, params, event_id=""):
        events.append({"name": event_name, "params": params, "event_id": event_id})

    monkeypatch.setattr(client, "report", fake_report)
    monkeypatch.setattr(voice_kpi.client, "report", fake_report)
    # Speaker un-muted unless a test says otherwise: app_state is process-wide
    # and another suite may have left the mute on.
    monkeypatch.setattr(voice_kpi, "_speaker_muted", lambda: False)
    voice_kpi.reset_for_test()
    yield events
    voice_kpi.reset_for_test()


@pytest.fixture
def fast_windows(monkeypatch):
    """Shrink the observation windows so tests do not sleep for 10s."""
    monkeypatch.setattr(voice_kpi, "ACK_OBSERVE_WINDOW_MS", 120)
    monkeypatch.setattr(voice_kpi, "STALE_GRACE_MS", 40)
    monkeypatch.setattr(voice_kpi, "STALE_OBSERVE_MS", 80)


def _wait_for(events, name, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for e in events:
            if e["name"] == name:
                return e
        time.sleep(0.01)
    raise AssertionError(f"no {name} event within {timeout}s: {events}")


class _FakeTTS:
    def __init__(self, native_mode=False, realtime_feedback=False,
                 interruptible=False, latest_queue_turn_id=""):
        self.native_mode = native_mode
        self.realtime_feedback = realtime_feedback
        self.interruptible = interruptible
        self.latest_queue_turn_id = latest_queue_turn_id


# --- KPI-1: acknowledgement --------------------------------------------------

def test_direct_realtime_reply_is_acknowledged(sent, fast_windows):
    """The realtime agent answers in its own voice — that IS the receipt."""
    iid = voice_kpi.speech_end("silence_clock")
    voice_kpi.set_route(iid, "handled", "voice_agent_handled")
    voice_kpi.playback_start_from_tts(_FakeTTS(native_mode=True))

    ev = _wait_for(sent, voice_kpi.EVENT_INTERACTION)
    p = ev["params"]
    assert p["outcome"] == voice_kpi.OUTCOME_ACKED
    assert p["eligible"] is True
    assert p["ack_modality"] == "spoken_answer_realtime"
    assert p["ack_latency_ms"] is not None and p["ack_latency_ms"] < voice_kpi.ACK_DEADLINE_MS
    assert p["speech_end_method"] == "silence_clock"


def test_delegated_reply_is_acknowledged_by_the_filler(sent, fast_windows):
    """Waiting audio is a user-perceivable receipt: the ack is the filler, and
    the later reply must not overwrite the measured latency."""
    iid = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(iid, "device-chat-7-1788422075499")
    voice_kpi.playback_start_from_tts(_FakeTTS(interruptible=True))
    first_latency = voice_kpi._interactions[iid].ack_latency_ms
    voice_kpi.playback_end()
    time.sleep(0.02)
    voice_kpi.playback_start_from_tts(
        _FakeTTS(realtime_feedback=True, latest_queue_turn_id="device-chat-7-1788422075499")
    )

    ev = _wait_for(sent, voice_kpi.EVENT_INTERACTION)
    p = ev["params"]
    assert p["ack_modality"] == "waiting_audio"
    assert p["ack_latency_ms"] == first_latency
    assert p["run_id"] == "device-chat-7-1788422075499"


def test_agent_reply_is_attributed_by_run_id(sent, fast_windows):
    """A reply arriving while a NEWER utterance is open still belongs to the
    turn that asked for it."""
    old = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(old, "device-chat-1-1788422075000")
    voice_kpi.speech_end("silence_clock")  # newer, unanswered
    voice_kpi.playback_start_from_tts(
        _FakeTTS(realtime_feedback=True, latest_queue_turn_id="device-chat-1-1788422075000")
    )
    assert voice_kpi._interactions[old].ack_latency_ms is not None


def test_no_reply_is_reported_not_dropped(sent, fast_windows):
    """An eligible interaction nothing answered must show up as no_ack —
    never as a missing row."""
    voice_kpi.speech_end("silence_clock")
    p = _wait_for(sent, voice_kpi.EVENT_INTERACTION)["params"]
    assert p["outcome"] == voice_kpi.OUTCOME_NO_ACK
    assert p["eligible"] is True
    assert p["ack_latency_ms"] is None


def test_slow_reply_keeps_its_real_latency(sent, fast_windows, monkeypatch):
    """A late answer is an observation with a number, not a discarded sample:
    the threshold has to be re-decidable from the data."""
    monkeypatch.setattr(voice_kpi, "ACK_OBSERVE_WINDOW_MS", 400)
    iid = voice_kpi.speech_end("silence_clock")
    time.sleep(0.15)
    voice_kpi.playback_start_from_tts(_FakeTTS(native_mode=True))
    p = _wait_for(sent, voice_kpi.EVENT_INTERACTION)["params"]
    assert p["outcome"] == voice_kpi.OUTCOME_ACKED
    assert p["ack_latency_ms"] >= 140
    assert p["ack_deadline_ms"] == voice_kpi.ACK_DEADLINE_MS
    assert iid == p["interaction_id"]


# --- Exclusions --------------------------------------------------------------

@pytest.mark.parametrize("reason", [
    voice_kpi.EXCL_REJECTED_NOISE,
    voice_kpi.EXCL_REJECTED_NON_USER,
    voice_kpi.EXCL_NO_TRANSCRIPT,
    voice_kpi.EXCL_NOT_ADDRESSED,
])
def test_rejected_input_is_excluded_with_a_reason(sent, fast_windows, reason):
    iid = voice_kpi.speech_end("silence_clock")
    voice_kpi.exclude(iid, reason)
    p = _wait_for(sent, voice_kpi.EVENT_INTERACTION)["params"]
    assert p["outcome"] == voice_kpi.OUTCOME_EXCLUDED
    assert p["eligible"] is False
    assert p["exclusion_reason"] == reason


def test_muted_speaker_is_excluded_not_counted_as_missing(sent, fast_windows, monkeypatch):
    monkeypatch.setattr(voice_kpi, "_speaker_muted", lambda: True)
    voice_kpi.speech_end("silence_clock")
    p = _wait_for(sent, voice_kpi.EVENT_INTERACTION)["params"]
    assert p["exclusion_reason"] == voice_kpi.EXCL_SPEAKER_MUTED
    assert p["eligible"] is False


def test_realtime_handled_does_not_create_a_second_sample(sent, fast_windows):
    """voice_agent_handled is a backend notification about an interaction that
    was already answered — it binds to the same one."""
    iid = voice_kpi.speech_end("silence_clock")
    voice_kpi.playback_start_from_tts(_FakeTTS(native_mode=True))
    voice_kpi.bind_run(iid, "device-chat-9-1788422075499")  # the handled POST
    voice_kpi.boundary(voice_kpi.BOUNDARY_AUTO_SUPERSEDE, iid)

    _wait_for(sent, voice_kpi.EVENT_INTERACTION)
    interactions = [e for e in sent if e["name"] == voice_kpi.EVENT_INTERACTION]
    assert len(interactions) == 1


# --- KPI-2: stale playback ---------------------------------------------------

def test_correct_suppression_reports_no_stale_playback(sent, fast_windows):
    """The old turn goes quiet: a suppression that worked, not a failure."""
    old = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(old, "run-old")
    new = voice_kpi.speech_end("silence_clock")
    voice_kpi.boundary(voice_kpi.BOUNDARY_AUTO_SUPERSEDE, new)

    p = _wait_for(sent, voice_kpi.EVENT_SUPPRESSION)["params"]
    assert p["stale_observed"] is False
    assert p["suppression_reason"] == voice_kpi.BOUNDARY_AUTO_SUPERSEDE
    assert p["applicable_interactions"] == 1


def test_stale_reply_after_the_grace_is_detected(sent, fast_windows):
    old = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(old, "run-old")
    new = voice_kpi.speech_end("silence_clock")
    voice_kpi.boundary(voice_kpi.BOUNDARY_AUTO_SUPERSEDE, new)

    time.sleep((voice_kpi.STALE_GRACE_MS + 20) / 1000.0)
    voice_kpi.playback_start_from_tts(
        _FakeTTS(realtime_feedback=True, latest_queue_turn_id="run-old")
    )

    p = _wait_for(sent, voice_kpi.EVENT_SUPPRESSION)["params"]
    assert p["stale_observed"] is True
    assert p["stale_kind"] == voice_kpi.KIND_AGENT_REPLY
    assert p["stale_started_after_ms"] >= voice_kpi.STALE_GRACE_MS


def test_stale_filler_counts_too(sent, fast_windows):
    """A filler for a superseded turn is stale speech like any other."""
    old = voice_kpi.speech_end("silence_clock")
    new = voice_kpi.speech_end("silence_clock")
    voice_kpi.boundary(voice_kpi.BOUNDARY_AUTO_SUPERSEDE, new)
    time.sleep((voice_kpi.STALE_GRACE_MS + 20) / 1000.0)
    voice_kpi.playback_start(voice_kpi.KIND_WAITING_AUDIO)
    # Attribute it to the old interaction the way the filler path does.
    voice_kpi._playing["interaction_id"] = old
    voice_kpi._observe_playback_for_boundaries(
        voice_kpi.KIND_WAITING_AUDIO, old, voice_kpi._now()
    )

    p = _wait_for(sent, voice_kpi.EVENT_SUPPRESSION)["params"]
    assert p["stale_observed"] is True
    assert p["stale_kind"] == voice_kpi.KIND_WAITING_AUDIO


def test_audio_inside_the_grace_is_not_stale(sent, fast_windows):
    """Playback does not stop the instant the boundary is stamped; the
    documented grace must not be counted as a failure."""
    old = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(old, "run-old")
    new = voice_kpi.speech_end("silence_clock")
    voice_kpi.playback_start_from_tts(
        _FakeTTS(realtime_feedback=True, latest_queue_turn_id="run-old")
    )
    voice_kpi.boundary(voice_kpi.BOUNDARY_AUTO_SUPERSEDE, new)
    voice_kpi.playback_end()  # goes quiet well inside the grace

    p = _wait_for(sent, voice_kpi.EVENT_SUPPRESSION)["params"]
    assert p["stale_observed"] is False
    assert p["old_audio_playing_at_boundary"] is True
    assert p["stop_to_silence_ms"] is not None
    assert p["grace_ms"] == voice_kpi.STALE_GRACE_MS


def test_explicit_stop_is_its_own_boundary(sent, fast_windows):
    """The click has different semantics from automatic supersession and is
    reported separately — it covers every turn in flight."""
    voice_kpi.speech_end("silence_clock")
    voice_kpi.speech_end("silence_clock")
    voice_kpi.boundary(voice_kpi.BOUNDARY_EXPLICIT_STOP)

    p = _wait_for(sent, voice_kpi.EVENT_SUPPRESSION)["params"]
    assert p["suppression_reason"] == voice_kpi.BOUNDARY_EXPLICIT_STOP
    assert p["applicable_interactions"] == 2


# --- Transport ---------------------------------------------------------------

def test_report_never_raises_into_the_voice_path(monkeypatch):
    """A broken pipe must not propagate into whoever was speaking."""
    def boom(*_a, **_k):
        raise RuntimeError("queue exploded")

    monkeypatch.setattr(client, "_ensure_worker", boom)
    client.report("voice_kpi_interaction", {"x": 1})  # must not raise
