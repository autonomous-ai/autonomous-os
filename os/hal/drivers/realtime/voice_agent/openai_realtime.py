"""OpenAI Realtime voice agent implementation — queue-based threading, fully sync."""

import base64
import logging
import queue
import threading
import time
from typing import Any, override

import cv2
import numpy as np
from openai import OpenAI
from openai.resources.realtime.realtime import RealtimeConnection

from hal.drivers.realtime.config import OpenAIConfig
from hal.drivers.realtime.enums import OpenAITurnDetectionType
from hal.drivers.realtime.exceptions import OpenAIRealtimeError
from hal.drivers.realtime.models import (
    AgentInputEvent,
    AudioCommitEvent,
    AudioInput,
    AudioOutput,
    FunctionCallOutput,
    FunctionCallResultInput,
    ImageInput,
    InputBase,
    InputEvent,
    OutputEvent,
    TextInput,
    TextOutput,
    TurnDoneEvent,
)
from hal.drivers.realtime.utils import (
    base64_pcm16_to_float32,
    float32_to_base64_pcm16,
)
from hal.drivers.realtime.voice_agent.base import VoiceAgentBase

logger = logging.getLogger(__name__)


class OpenAIRealtimeAgent(VoiceAgentBase):
    def __init__(
        self,
        config: OpenAIConfig,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(tools=tools)
        self._config: OpenAIConfig = config
        self._client: OpenAI = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        self._connection: RealtimeConnection | None = None
        # Serializes all access to self._connection across the send/recv threads.
        # Reentrant: a send op holds it while triggering _safe_response_create,
        # which re-acquires it on the same thread. The blocking recv iteration
        # runs OUTSIDE the lock (on a snapshot) so sends aren't starved during a
        # turn — only connection swaps, writes, and teardown are serialized.
        self._conn_lock: threading.RLock = threading.RLock()
        self._speech_ended_at: float | None = None
        self._reconnect_delay_s: float = config.reconnect_delay_s
        self._max_retries: int = config.max_retries
        self._last_reconnect_at: float = 0.0
        # Signals that the model is idle (no active response). Set by default,
        # cleared when response.create() is called, set again on response.done.
        self._turn_done: threading.Event = threading.Event()
        self._turn_done.set()

    @property
    @override
    def sample_rate(self) -> int:
        return self._config.sample_rate

    # --- Sync internals ---

    def _sync_connect(self) -> None:
        logger.info(
            "Connecting to OpenAI Realtime API (base_url=%s, model=%s)",
            self._config.base_url,
            self._config.model,
        )

        self._connection = self._client.realtime.connect(
            model=self._config.model,
        ).enter()

        turn_detection: dict[str, str] | None = None
        if self._config.turn_detection_type is not None:
            td_type: OpenAITurnDetectionType = self._config.turn_detection_type
            if td_type == OpenAITurnDetectionType.SERVER_VAD:
                turn_detection = {"type": "server_vad"}
            elif td_type == OpenAITurnDetectionType.SEMANTIC_VAD:
                turn_detection = {"type": "semantic_vad"}

        session_config: dict[str, Any] = {
            "type": "realtime",
            "instructions": self._config.instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": self._config.sample_rate},
                    "turn_detection": turn_detection,
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": self._config.sample_rate},
                    "voice": self._config.voice.value,
                },
            },
        }

        if self._tools:
            session_config["tools"] = self._tools
            session_config["tool_choice"] = "auto"

        if self._config.reasoning_effort is not None:
            session_config["reasoning"] = {
                "effort": self._config.reasoning_effort.value,
            }

        truncation_cfg: dict[str, Any] = {"type": self._config.truncation_type.value}
        if self._config.truncation_type.value == "retention_ratio":
            truncation_cfg["retention_ratio"] = self._config.truncation_retention_ratio
        session_config["truncation"] = truncation_cfg

        self._connection.session.update(session=session_config)
        logger.info("[realtime] OpenAI Realtime session open (voice=%s)", self._config.voice)

    def _sync_disconnect(self) -> None:
        if self._connection is not None:
            logger.info("[realtime] Disconnecting from OpenAI Realtime API")
            self._connection.close()
            self._connection = None

    def _sync_send_input(self, input: InputBase) -> None:
        with self._conn_lock:
            if self._connection is None:
                return

            if isinstance(input, AudioInput):
                b64_audio: str = float32_to_base64_pcm16(input.audio)
                self._connection.input_audio_buffer.append(audio=b64_audio)

            elif isinstance(input, TextInput):
                self._connection.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": input.text}],
                    }
                )

            elif isinstance(input, ImageInput):
                _: bool
                buf: np.ndarray
                _, buf = cv2.imencode(".png", input.image)
                b64_img: str = base64.b64encode(buf.tobytes()).decode("ascii")
                data_uri: str = f"data:image/png;base64,{b64_img}"
                self._connection.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_image", "image_url": data_uri}],
                    }
                )

            elif isinstance(input, FunctionCallResultInput):
                self._connection.conversation.item.create(
                    item={
                        "type": "function_call_output",
                        "call_id": input.call_id,
                        "output": input.output,
                    }
                )
                # Fire-and-forget tools (trigger_response=False) only record the
                # result; they must NOT spawn a fresh response or the model would
                # speak a second time and add a full round-trip of latency.
                if input.trigger_response:
                    self._safe_response_create()

    def _sync_commit(self) -> None:
        with self._conn_lock:
            if self._connection is None:
                return
            self._connection.input_audio_buffer.commit()
            self._safe_response_create()

    def _safe_response_create(self) -> None:
        """Wait for any active response to finish, then create a new one.

        The wait runs before taking the connection lock so the recv thread can
        keep draining events (and set _turn_done) without contending with us.
        """
        if not self._turn_done.wait(timeout=10.0):
            logger.warning("[realtime] Timed out waiting for active response to finish — forcing new response")
        with self._conn_lock:
            if self._connection is None:
                return
            self._turn_done.clear()
            self._speech_ended_at = time.monotonic()
            self._connection.response.create()

    def _sync_receive_turn(self, conn: RealtimeConnection) -> None:
        """Read one full turn from `conn`, put outputs on _recv_queue.

        Iterates on the caller-supplied connection snapshot (not self._connection)
        so a concurrent reconnect that swaps the connection can't be read mid-turn.
        """
        for event in conn:
            match event.type:
                case "input_audio_buffer.speech_stopped":
                    self._speech_ended_at = time.monotonic()

                case "response.output_text.delta":
                    self._recv_queue.put(
                        OutputEvent(output=TextOutput(text=event.delta))
                    )

                case "response.output_audio.delta":
                    if self._speech_ended_at is not None:
                        latency_ms: float = (
                            time.monotonic() - self._speech_ended_at
                        ) * 1000
                        logger.info("[realtime] Response latency: %.0fms", latency_ms)
                        self._speech_ended_at = None
                    self._recv_queue.put(
                        OutputEvent(
                            output=AudioOutput(
                                audio=base64_pcm16_to_float32(event.delta)
                            ),
                        )
                    )

                case "response.output_audio_transcript.delta":
                    self._recv_queue.put(
                        OutputEvent(output=TextOutput(text=event.delta))
                    )

                case "response.function_call_arguments.done":
                    logger.debug(
                        "Function call: %s (call_id=%s)", event.name, event.call_id
                    )
                    self._recv_queue.put(
                        OutputEvent(
                            output=FunctionCallOutput(
                                name=event.name,
                                arguments=event.arguments,
                                call_id=event.call_id,
                            ),
                        )
                    )

                case "response.done":
                    logger.debug("[realtime] Response complete")
                    # Per-turn token bill. input_tokens is the input CONTEXT billed
                    # this turn — it grows as a long-lived session accumulates
                    # history and should drop right after an idle session recycle
                    # (see orchestrator._mark_turn_start). cached covers prompt
                    # caching. Grep "[realtime] OpenAI usage" to confirm cost cut.
                    usage = getattr(getattr(event, "response", None), "usage", None)
                    if usage is not None:
                        details = getattr(usage, "input_token_details", None)
                        logger.info(
                            "[realtime] OpenAI usage: input(context)=%s output=%s "
                            "total=%s cached=%s",
                            getattr(usage, "input_tokens", None),
                            getattr(usage, "output_tokens", None),
                            getattr(usage, "total_tokens", None),
                            getattr(details, "cached_tokens", None),
                        )
                    self._turn_done.set()
                    self._recv_queue.put(TurnDoneEvent())
                    return

                case "error":
                    logger.error("[realtime] Realtime API error: %s", event.error)
                    raise OpenAIRealtimeError(f"Realtime API error: {event.error}")

                case _:
                    pass

    # --- Reconnect ---

    def _ensure_connected(self) -> None:
        """Reconnect if not connected. Throttled to at most once per reconnect_delay_s."""
        if self._connected.is_set():
            return
        now: float = time.monotonic()
        if now - self._last_reconnect_at < self._reconnect_delay_s:
            return
        self._last_reconnect_at = now
        self._reconnect()

    def _reconnect(self) -> None:
        with self._conn_lock:
            # Another thread may have reconnected while we waited for the lock —
            # don't tear a healthy connection back down.
            if self._connected.is_set():
                return
            self._turn_done.set()  # unblock any waiting commit
            try:
                logger.info("[realtime] Reconnecting...")
                self._sync_disconnect()
                self._sync_connect()
                self._connected.set()
            except Exception as e:
                logger.warning("[realtime] Reconnect failed: %s — will retry on next audio", e)

    def _drop_connection(self, conn: RealtimeConnection | None) -> None:
        """Mark the connection dead — only if `conn` is still the current one.

        Both loops call this on error; the identity check stops one thread from
        nulling a connection the other thread just re-established.
        """
        with self._conn_lock:
            if conn is None or self._connection is conn:
                self._connected.clear()
                self._connection = None

    # --- VoiceAgentBase implementation ---

    @override
    def _do_connect(self) -> None:
        self._sync_connect()

    @override
    def _do_disconnect(self) -> None:
        self._sync_disconnect()

    @override
    def _send_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                event: AgentInputEvent = self._send_queue.get(timeout=1)
            except queue.Empty:
                continue

            for attempt in range(self._max_retries):
                self._ensure_connected()
                if not self._connected.is_set():
                    logger.debug("[realtime] Not connected, skipping attempt %d/%d", attempt + 1, self._max_retries)
                    continue
                conn: RealtimeConnection | None = self._connection
                try:
                    if isinstance(event, AudioCommitEvent):
                        self._sync_commit()
                    elif isinstance(event, InputEvent) and event.input is not None:
                        self._sync_send_input(event.input)
                    break  # Success
                except Exception as e:
                    logger.exception("[realtime] Send failed (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._drop_connection(conn)

    @override
    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._connected.is_set():
                self._connected.wait(timeout=1)
                continue

            for attempt in range(self._max_retries):
                self._ensure_connected()
                with self._conn_lock:
                    conn: RealtimeConnection | None = (
                        self._connection if self._connected.is_set() else None
                    )
                if conn is None:
                    logger.debug("[realtime] Not connected, skipping attempt %d/%d", attempt + 1, self._max_retries)
                    continue
                try:
                    self._sync_receive_turn(conn)
                    break  # Success
                except OpenAIRealtimeError as e:
                    logger.warning("[realtime] Recv failed (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._drop_connection(conn)
                except Exception as e:
                    logger.exception("[realtime] Unexpected recv error (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._drop_connection(conn)
