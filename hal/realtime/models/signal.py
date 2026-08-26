from pydantic import BaseModel


class DelegateSignal(BaseModel):
    """Yielded by stream_output() when the model calls delegate_to_main."""

    message: str = ""


class RejectSignal(BaseModel):
    """Yielded when the model explicitly rejects a non-user turn.

    This is intentionally distinct from a turn that merely ends with no output:
    only this signal is allowed to suppress the normal main-agent fallback.
    """


class LookReplaySignal(BaseModel):
    """Yielded by stream_output() when the model called `look` and a FRESH
    camera frame was sent. The Live API queues a frame sent mid-turn for the
    NEXT turn (device-proven 2026-07-02: the model answered every look from
    the previous look's image), so the turn driver must replay the user's
    audio as a new turn — that new turn picks up the queued frame, and the
    model answers about what the user is holding NOW."""
