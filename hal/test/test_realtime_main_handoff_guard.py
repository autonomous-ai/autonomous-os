"""The in-flight main handoff guard (#419).

While the main agent is working on a delegated request, the realtime layer
must not answer a nudge from memory. The tracker is the deterministic state
behind that: opened on delegation, closed when the reply lands / the user
clicks / the TTL passes.
"""

from hal.realtime.main_handoff import MainHandoffTracker


def _tracker(ttl_s: float = 120.0, gap_s: float = 4.0) -> MainHandoffTracker:
    return MainHandoffTracker(ttl_s=ttl_s, filler_gap_s=gap_s)


def test_closed_by_default():
    t = _tracker()
    assert not t.is_open(now=10.0)
    assert t.transcript() == ""


def test_open_then_reply_closes():
    t = _tracker()
    t.open("Find my pen", now=10.0)
    assert t.is_open(now=30.0)
    assert t.transcript() == "Find my pen"

    assert t.close("main_reply", now=31.0) is True
    assert not t.is_open(now=31.0)
    assert t.transcript() == ""


def test_close_when_already_closed_reports_false():
    t = _tracker()
    assert t.close("click", now=1.0) is False


def test_ttl_expires_the_handoff():
    t = _tracker(ttl_s=120.0)
    t.open("Find my pen", now=10.0)
    assert t.is_open(now=129.9)
    assert not t.is_open(now=130.0)


def test_ttl_zero_disables_the_guard():
    t = _tracker(ttl_s=0)
    t.open("Find my pen", now=10.0)
    assert not t.is_open(now=10.0)


def test_reopen_restarts_the_ttl():
    t = _tracker(ttl_s=60.0)
    t.open("first", now=0.0)
    t.open("second", now=50.0)
    assert t.is_open(now=100.0)
    assert t.transcript() == "second"


def test_filler_slot_is_rate_limited():
    t = _tracker(gap_s=4.0)
    t.open("Find my pen", now=0.0)
    assert t.take_filler_slot(now=1.0) is True
    assert t.take_filler_slot(now=3.0) is False
    assert t.take_filler_slot(now=5.0) is True


def test_filler_slot_never_granted_while_closed():
    t = _tracker()
    assert t.take_filler_slot(now=1.0) is False
