"""Abstract base class for realtime voice agents — sync, queue-based."""

import logging
import queue
import threading
from abc import ABC, abstractmethod
from collections.abc import Generator
from typing import Any

import numpy as np
import numpy.typing as npt

from hal import config as app_config
from hal.drivers.realtime.models import (
    AgentInputEvent,
    AgentOutputEvent,
    AudioCommitEvent,
    AudioInput,
    InputBase,
    InputEvent,
    OutputBase,
    OutputEvent,
    TurnDoneEvent,
)

logger = logging.getLogger(__name__)


class VoiceAgentBase(ABC):
    """Sync interface for a realtime voice agent.

    Lifecycle: connect → (append_audio / commit_audio / send + receive) → disconnect.

    Internally each provider runs two threads:
      - _send_loop: drains _send_queue → API (reconnects on error)
      - _recv_loop: API → _recv_queue (reconnects on error)

    Public methods are non-blocking (queue puts) except connect/disconnect/receive.
    """

    def __init__(self, tools: list[dict[str, Any]] | None = None):
        self._tools: list[dict[str, Any]] = tools or []
        self._send_queue: queue.Queue[AgentInputEvent] = queue.Queue()
        self._recv_queue: queue.Queue[AgentOutputEvent] = queue.Queue()
        self._connected = threading.Event()
        self._stop_event = threading.Event()
        self._send_thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None
        # Armed by skip_next_turn_done() (look replay): swallow ONE stale
        # TurnDoneEvent that arrives before any real output. See receive().
        self._skip_stale_turn_done: bool = False

    @property
    def available(self) -> bool:
        """Whether the agent is connected and ready."""
        return self._connected.is_set()

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Sample rate expected by this provider (Hz) — the INPUT/mic rate."""

    @property
    def output_sample_rate(self) -> int:
        """Sample rate of the model's OWN audio output (Hz), for native playback.
        Defaults to the input rate; providers override when they differ."""
        return self.sample_rate

    def connect(self) -> None:
        """Connect to the provider and start send/recv loops."""
        self._stop_event.clear()
        self._do_connect()
        self._connected.set()
        self._send_thread = threading.Thread(
            target=self._send_loop, daemon=True, name="rt-send",
        )
        self._recv_thread = threading.Thread(
            target=self._recv_loop, daemon=True, name="rt-recv",
        )
        self._send_thread.start()
        self._recv_thread.start()

    def disconnect(self) -> None:
        """Stop loops and disconnect."""
        self._stop_event.set()
        self._connected.clear()
        if self._send_thread is not None:
            self._send_thread.join(timeout=5)
            self._send_thread = None
        if self._recv_thread is not None:
            self._recv_thread.join(timeout=5)
            self._recv_thread = None
        self._do_disconnect()

    def append_audio(self, audio: npt.NDArray[np.float32]) -> None:
        """Queue a single audio frame for sending (non-blocking)."""
        if self.available:
            self._send_queue.put(InputEvent(input=AudioInput(audio=audio)))

    def commit_audio(self) -> None:
        """Queue a commit signal (non-blocking)."""
        if self.available:
            self._send_queue.put(AudioCommitEvent())

    def flush_output(self) -> None:
        """Drop any output events left on the recv queue from a previous turn.

        Provider responses land on `_recv_queue` asynchronously and can lag the
        caller's local-VAD turn cadence. If the queue is not cleared, the next
        turn's `receive()` reads a STALE prior response (read in milliseconds,
        well before this turn's real reply) and speaks it — the "agent talks on
        its own after a noise blip" + double-reply bug. Call right before
        `commit_audio()` so every turn starts from an empty queue and only ever
        reads its own response.
        """
        dropped = 0
        while True:
            try:
                self._recv_queue.get_nowait()
            except queue.Empty:
                break
            dropped += 1
        if dropped:
            logger.info(
                "[realtime] Flushed %d stale output event(s) before new turn", dropped
            )

    def send(self, inputs: list[InputBase]) -> None:
        """Queue inputs for sending (non-blocking)."""
        if self.available:
            for inp in inputs:
                self._send_queue.put(InputEvent(input=inp))

    def end_turn(self) -> None:
        """Mark the current turn finished from the consumer's side.

        Default no-op. Gemini's manual-VAD path gates the NEXT turn's commit on a
        `_turn_done` event that only the model's `turn_complete` sets. When a turn
        ends with a tool call (e.g. delegate_to_main) the model sends no
        turn_complete, so without this the next commit blocks on `_turn_done.wait`
        for the full 10s timeout. The orchestrator calls this after a delegate so
        the following turn commits immediately.
        """

    def skip_next_turn_done(self) -> None:
        """Arm receive() to swallow ONE TurnDoneEvent that arrives before any
        real output.

        Look replay: the orchestrator cancels the model's tool-call turn by
        re-committing the user's audio, but the server's turn_complete for the
        CANCELLED turn lands on the recv queue a few hundred ms later — after
        flush_output() already ran for the replay — and receive() would break
        on it, ending the replayed turn empty (device-observed: replay died
        ~340ms after commit). The flag disarms on the first real output, so a
        turn_complete that follows actual content (the live turn's own) is
        never swallowed.
        """
        self._skip_stale_turn_done = True

    def receive(self, *, stop_on_done: bool = True) -> Generator[OutputBase, None, None]:
        """Sync generator — yields OutputBase items from _recv_queue.

        When stop_on_done=True (default), stops at the first TurnDoneEvent.
        When stop_on_done=False, skips TurnDoneEvents and keeps yielding across turns.
        """
        while True:
            try:
                event = self._recv_queue.get(
                    timeout=app_config.REALTIME_RECV_QUEUE_TIMEOUT_S
                )
            except queue.Empty:
                # No output within the gap window — almost always the model
                # staying silent on a noise / non-directed turn (correct), not an
                # error. End the turn quietly so it can fall back to the main agent.
                logger.info(
                    "receive() got no output within %.1fs — ending turn (model stayed silent)",
                    app_config.REALTIME_RECV_QUEUE_TIMEOUT_S,
                )
                break
            if isinstance(event, TurnDoneEvent):
                if self._skip_stale_turn_done:
                    self._skip_stale_turn_done = False
                    logger.info(
                        "[realtime] swallowed stale turn_complete from a cancelled turn"
                    )
                    continue
                if stop_on_done:
                    break
                continue
            if isinstance(event, OutputEvent):
                self._skip_stale_turn_done = False  # real output → next done is live
                yield event.output

    # --- Abstract: provider-specific implementation ---

    @abstractmethod
    def _do_connect(self) -> None:
        """Establish the WebSocket/API connection. Called from connect()."""

    @abstractmethod
    def _do_disconnect(self) -> None:
        """Close the WebSocket/API connection. Called from disconnect()."""

    @abstractmethod
    def _send_loop(self) -> None:
        """Thread target: drain _send_queue, send to API. Reconnect on error."""

    @abstractmethod
    def _recv_loop(self) -> None:
        """Thread target: read from API, put on _recv_queue. Reconnect on error."""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False
