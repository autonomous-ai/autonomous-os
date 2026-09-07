"""Voice KPI measurement tests.

Every test uses a mock transport and a fake clock — no event leaves the
process, no test contacts the production analytics endpoint, and no test
sleeps for a real observation window.
"""

import threading

import pytest

from hal.tracking import client, voice_kpi


class FakeClock:
    """Monotonic clock the tests drive by hand."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, ms):
        self.t += ms / 1000.0


@pytest.fixture
def kpi(monkeypatch):
    """voice_kpi with a mock transport, a fake clock, and no real timers.

    Timers are captured instead of started so a test closes an observation
    window explicitly — that is what makes "the window expired while the turn
    was still alive" testable at all.
    """
    events = []
    timers = []

    def fake_report(event_name, params, event_id=""):
        events.append({"name": event_name, "params": params, "event_id": event_id})

    class FakeTimer:
        def __init__(self, interval, function, args=(), kwargs=None):
            self.interval, self.function, self.args = interval, function, args or ()
            self.daemon = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            if self in timers:
                timers.remove(self)

        def fire(self):
            self.cancel()
            self.function(*self.args)

    clock = FakeClock()
    monkeypatch.setattr(voice_kpi.client, "report", fake_report)
    monkeypatch.setattr(client, "report", fake_report)
    monkeypatch.setattr(voice_kpi, "_now", clock)
    monkeypatch.setattr(voice_kpi.threading, "Timer", FakeTimer)
    monkeypatch.setattr(voice_kpi, "_speaker_muted", lambda: False)
    voice_kpi.reset_for_test()

    class Harness:
        def __init__(self):
            self.events, self.timers, self.clock = events, timers, clock

        def close_all(self):
            for t in list(timers):
                t.fire()

        def of(self, name):
            return [e for e in self.events if e["name"] == name]

        def one(self, name):
            rows = self.of(name)
            assert len(rows) == 1, f"expected 1 {name}, got {len(rows)}: {rows}"
            return rows[0]["params"]

    yield Harness()
    voice_kpi.reset_for_test()


class FakeTTS:
    def __init__(self, realtime_feedback=False, interruptible=False):
        self.realtime_feedback = realtime_feedback
        self.interruptible = interruptible


def _reply(kpi, owner):
    voice_kpi.playback_audio(owner, "agent_or_system", FakeTTS(realtime_feedback=True))


def _filler(kpi, owner):
    voice_kpi.playback_audio(owner, "cached", FakeTTS(interruptible=True))


def _native(kpi, owner):
    voice_kpi.playback_audio(owner, "native_realtime", FakeTTS())


# --- Finding 1: ownership, no guessing --------------------------------------

def test_unowned_audio_is_never_an_acknowledgement(kpi):
    """Regression: audio nobody claimed used to be credited to the newest open
    interaction, so an old filler could 'acknowledge' a new command."""
    voice_kpi.speech_end("silence_clock")
    kpi.clock.advance(500)
    voice_kpi.playback_audio("", "cached", FakeTTS(interruptible=True))
    kpi.close_all()

    p = kpi.one(voice_kpi.EVENT_INTERACTION)
    assert p["outcome"] == voice_kpi.OUTCOME_NO_ACK
    assert p["ack_latency_ms"] is None
    assert p["unknown_owner_playbacks"] == 1


def test_old_turn_audio_does_not_acknowledge_a_new_command(kpi):
    """Explicit ownership sends the filler to the turn it belongs to."""
    old = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(old, "run-old")
    kpi.clock.advance(1000)
    new = voice_kpi.speech_end("silence_clock")
    kpi.clock.advance(500)
    _filler(kpi, "run:run-old")
    kpi.close_all()

    rows = {e["params"]["interaction_id"]: e["params"] for e in kpi.of(voice_kpi.EVENT_INTERACTION)}
    assert rows[old]["outcome"] == voice_kpi.OUTCOME_ACKED
    assert rows[new]["outcome"] == voice_kpi.OUTCOME_NO_ACK


def test_realtime_native_audio_is_owned_by_its_interaction(kpi):
    iid = voice_kpi.speech_end("silence_clock")
    kpi.clock.advance(800)
    _native(kpi, f"interaction:{iid}")
    kpi.close_all()

    p = kpi.one(voice_kpi.EVENT_INTERACTION)
    assert p["ack_modality"] == "spoken_answer_realtime"
    assert p["ack_latency_ms"] == 800


# --- Finding 2: long-lived suppression watching ------------------------------

def test_stale_reply_after_five_seconds_is_still_detected(kpi):
    """Regression: the watcher used to close after grace+3s, so a main-agent
    reply arriving later was scored as a clean suppression."""
    old = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(old, "run-old")
    kpi.clock.advance(1000)
    new = voice_kpi.speech_end("silence_clock")
    voice_kpi.boundary(voice_kpi.BOUNDARY_AUTO_SUPERSEDE, new)

    kpi.clock.advance(20000)          # far past the old 5s window
    _reply(kpi, "run:run-old")
    kpi.close_all()

    p = kpi.of(voice_kpi.EVENT_SUPPRESSION)[0]["params"]
    assert p["stale_observed"] is True
    assert p["stale_kind"] == voice_kpi.KIND_AGENT_REPLY
    assert p["stale_started_after_ms"] == 20000


def test_window_closing_on_a_live_turn_reports_incomplete_not_clean(kpi):
    """An unfinished observation must not read as 'no stale reply'."""
    old = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(old, "run-old")
    kpi.clock.advance(1000)
    new = voice_kpi.speech_end("silence_clock")
    voice_kpi.boundary(voice_kpi.BOUNDARY_AUTO_SUPERSEDE, new)

    # Close ONLY the suppression watcher; the old turn is still alive.
    boundary_timer = kpi.timers[-1]
    boundary_timer.fire()

    p = kpi.of(voice_kpi.EVENT_SUPPRESSION)[0]["params"]
    assert p["stale_observed"] is False
    assert p["observation_complete"] is False
    assert p["unobserved_interactions"] == 1


def test_completed_turns_make_the_observation_complete(kpi):
    old = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(old, "run-old")
    kpi.clock.advance(1000)
    new = voice_kpi.speech_end("silence_clock")
    voice_kpi.boundary(voice_kpi.BOUNDARY_AUTO_SUPERSEDE, new)
    kpi.close_all()

    p = kpi.of(voice_kpi.EVENT_SUPPRESSION)[0]["params"]
    assert p["stale_observed"] is False
    assert p["observation_complete"] is True


# --- Finding 3: audio that CONTINUES past the grace --------------------------

def test_audio_continuing_past_the_grace_is_stale(kpi):
    """Regression: old audio playing at the stop and ending 3s later (grace 2s)
    reported stop_to_silence_ms=3000 but stale_observed=false."""
    old = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(old, "run-old")
    _reply(kpi, "run:run-old")
    kpi.clock.advance(500)
    voice_kpi.boundary(voice_kpi.BOUNDARY_EXPLICIT_STOP)

    kpi.clock.advance(3000)           # still talking 3s after the stop
    voice_kpi.playback_end()
    kpi.close_all()

    p = kpi.of(voice_kpi.EVENT_SUPPRESSION)[0]["params"]
    assert p["old_audio_playing_at_boundary"] is True
    assert p["stop_to_silence_ms"] == 3000
    assert p["stale_observed"] is True
    assert p["stale_audible_past_grace_ms"] == 3000 - voice_kpi.STALE_GRACE_MS


def test_audio_stopping_inside_the_grace_is_not_stale(kpi):
    """The documented grace stays a pass — and the raw timing is still kept."""
    old = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(old, "run-old")
    _reply(kpi, "run:run-old")
    kpi.clock.advance(500)
    voice_kpi.boundary(voice_kpi.BOUNDARY_EXPLICIT_STOP)

    kpi.clock.advance(800)            # quiet well inside STALE_GRACE_MS
    voice_kpi.playback_end()
    kpi.close_all()

    p = kpi.of(voice_kpi.EVENT_SUPPRESSION)[0]["params"]
    assert p["stale_observed"] is False
    assert p["stop_to_silence_ms"] == 800
    assert p["grace_ms"] == voice_kpi.STALE_GRACE_MS


def test_stale_filler_counts_too(kpi):
    old = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(old, "run-old")
    kpi.clock.advance(1000)
    new = voice_kpi.speech_end("silence_clock")
    voice_kpi.boundary(voice_kpi.BOUNDARY_AUTO_SUPERSEDE, new)
    kpi.clock.advance(voice_kpi.STALE_GRACE_MS + 500)
    _filler(kpi, "run:run-old")
    kpi.close_all()

    p = kpi.of(voice_kpi.EVENT_SUPPRESSION)[0]["params"]
    assert p["stale_observed"] is True
    assert p["stale_kind"] == voice_kpi.KIND_WAITING_AUDIO


# --- Finding 4: only real boundaries, only active turns ----------------------

def test_boundary_is_not_recorded_when_the_policy_did_not_apply(kpi):
    """OS_REALTIME_SUPERSEDES_MAIN_REPLY off (or a failed POST) means nothing
    was suppressed — not a KPI-2 situation at all."""
    old = voice_kpi.speech_end("silence_clock")
    kpi.clock.advance(1000)
    new = voice_kpi.speech_end("silence_clock")
    voice_kpi.boundary(voice_kpi.BOUNDARY_AUTO_SUPERSEDE, new, policy_applied=False)
    kpi.close_all()
    assert kpi.of(voice_kpi.EVENT_SUPPRESSION) == []
    assert old != new


def test_denominator_counts_only_active_turns(kpi):
    """A turn already excluded cannot produce a stale reply; keeping it in the
    denominator would dilute KPI-2 with situations that never existed."""
    done = voice_kpi.speech_end("silence_clock")
    voice_kpi.exclude(done, voice_kpi.EXCL_REJECTED_NOISE)
    kpi.clock.advance(500)
    live = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(live, "run-live")
    kpi.clock.advance(500)
    new = voice_kpi.speech_end("silence_clock")

    voice_kpi.boundary(voice_kpi.BOUNDARY_AUTO_SUPERSEDE, new)
    p_state = voice_kpi._watchers[0]
    assert p_state["applicable"] == {live}


def test_explicit_stop_covers_every_turn_in_flight(kpi):
    a = voice_kpi.speech_end("silence_clock")
    kpi.clock.advance(100)
    b = voice_kpi.speech_end("silence_clock")
    voice_kpi.boundary(voice_kpi.BOUNDARY_EXPLICIT_STOP)
    kpi.close_all()

    p = kpi.of(voice_kpi.EVENT_SUPPRESSION)[0]["params"]
    assert p["suppression_reason"] == voice_kpi.BOUNDARY_EXPLICIT_STOP
    assert p["applicable_interactions"] == 2
    assert a != b


# --- Finding 5: first real audio write, not the callback ---------------------

def test_speech_end_clock_starts_at_the_detected_endpoint(kpi):
    """Latency is measured from the endpoint detection, not from the moment
    the tracker was told about it (transcript assembly runs in between)."""
    detected_at = kpi.clock.t
    kpi.clock.advance(400)                       # finalize_session work
    voice_kpi.speech_end("silence_clock", at=detected_at)
    kpi.clock.advance(600)
    _native(kpi, "")                             # unowned: no ack
    iid = voice_kpi._order[-1]
    voice_kpi.bind_run(iid, "run-x")
    _reply(kpi, "run:run-x")
    kpi.close_all()

    p = kpi.one(voice_kpi.EVENT_INTERACTION)
    assert p["ack_latency_ms"] == 1000           # 400 + 600, not 600


# --- Outcomes and exclusions -------------------------------------------------

@pytest.mark.parametrize("reason", [
    voice_kpi.EXCL_REJECTED_NOISE,
    voice_kpi.EXCL_REJECTED_NON_USER,
    voice_kpi.EXCL_NO_TRANSCRIPT,
    voice_kpi.EXCL_NOT_ADDRESSED,
    voice_kpi.EXCL_DISPATCH_FAILED,
])
def test_excluded_inputs_are_reported_with_a_reason(kpi, reason):
    iid = voice_kpi.speech_end("silence_clock")
    voice_kpi.exclude(iid, reason)
    kpi.close_all()

    p = kpi.one(voice_kpi.EVENT_INTERACTION)
    assert p["outcome"] == voice_kpi.OUTCOME_EXCLUDED
    assert p["eligible"] is False
    assert p["exclusion_reason"] == reason


def test_muted_speaker_is_excluded_not_counted_as_missing(kpi, monkeypatch):
    monkeypatch.setattr(voice_kpi, "_speaker_muted", lambda: True)
    voice_kpi.speech_end("silence_clock")
    kpi.close_all()

    p = kpi.one(voice_kpi.EVENT_INTERACTION)
    assert p["exclusion_reason"] == voice_kpi.EXCL_SPEAKER_MUTED


def test_slow_reply_keeps_its_real_latency(kpi):
    iid = voice_kpi.speech_end("silence_clock")
    voice_kpi.bind_run(iid, "run-slow")
    kpi.clock.advance(7200)
    _reply(kpi, "run:run-slow")
    kpi.close_all()

    p = kpi.one(voice_kpi.EVENT_INTERACTION)
    assert p["outcome"] == voice_kpi.OUTCOME_ACKED
    assert p["ack_latency_ms"] == 7200            # eligible, just over target
    assert p["ack_deadline_ms"] == voice_kpi.ACK_DEADLINE_MS


def test_realtime_handled_is_counted_once(kpi):
    iid = voice_kpi.speech_end("silence_clock")
    kpi.clock.advance(600)
    _native(kpi, f"interaction:{iid}")
    voice_kpi.bind_run(iid, "run-handled")        # the voice_agent_handled POST
    voice_kpi.boundary(voice_kpi.BOUNDARY_AUTO_SUPERSEDE, iid)
    kpi.close_all()

    assert len(kpi.of(voice_kpi.EVENT_INTERACTION)) == 1


def test_late_exclusion_amends_an_already_reported_verdict(kpi):
    """A verdict that turns out wrong must be corrected in the warehouse, not
    left standing."""
    iid = voice_kpi.speech_end("silence_clock")
    kpi.close_all()                                # verdict reported: no_ack
    voice_kpi.exclude(iid, voice_kpi.EXCL_NOT_ADDRESSED)

    rows = kpi.of(voice_kpi.EVENT_INTERACTION)
    assert len(rows) == 2
    amendment = rows[1]["params"]
    assert amendment["amends_event_id"] == "int-" + iid
    assert amendment["amendment_reason"] == "late_exclusion"
    assert amendment["exclusion_reason"] == voice_kpi.EXCL_NOT_ADDRESSED


def test_duplicate_report_is_suppressed_by_event_id(kpi):
    iid = voice_kpi.speech_end("silence_clock")
    voice_kpi._close_interaction(iid)
    voice_kpi._close_interaction(iid)
    assert len(kpi.of(voice_kpi.EVENT_INTERACTION)) == 1


# --- Transport ---------------------------------------------------------------

def test_report_never_raises_into_the_voice_path(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("queue exploded")

    monkeypatch.setattr(client, "_ensure_worker", boom)
    client.report("voice_kpi_interaction", {"x": 1})    # must not raise


def test_report_does_not_block_the_caller(monkeypatch):
    """Telemetry stays off the audio critical path: a full queue drops."""
    monkeypatch.setattr(client, "_ensure_worker", lambda: None)
    for _ in range(client.QUEUE_SIZE + 5):
        client.report("voice_kpi_interaction", {"x": 1})
    assert client.stats()["dropped"] >= 1
    assert threading.current_thread() is threading.main_thread()
