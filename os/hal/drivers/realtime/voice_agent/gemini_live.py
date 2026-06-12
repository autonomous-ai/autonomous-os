"""Gemini Live voice agent implementation — queue-based threading."""

import asyncio
import json
import logging
import queue
import threading
import time
from contextlib import AsyncExitStack
from typing import Any, override

import cv2
import google.genai as genai
import numpy as np
import numpy.typing as npt
from google.genai import errors as genai_errors
from google.genai import types
from google.genai.live import AsyncSession
from websockets.exceptions import ConnectionClosed

from hal.drivers.realtime.config import GeminiConfig
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
from hal.drivers.realtime.utils import float32_to_pcm16_bytes, pcm16_bytes_to_float32
from hal.drivers.realtime.voice_agent.base import VoiceAgentBase

logger = logging.getLogger(__name__)


class GeminiLiveAgent(VoiceAgentBase):
    DEFAULT_RECONNECT_DELAY_S: float = 2.0
    DEFAULT_SEND_TIMEOUT_S: float = 10.0
    DEFAULT_RECV_TIMEOUT_S: float = 300.0
    DEFAULT_QUEUE_POLL_S: float = 1.0
    DEFAULT_JOIN_TIMEOUT_S: float = 5.0

    def __init__(
        self,
        config: GeminiConfig,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(tools=tools)
        self._config: GeminiConfig = config
        client_kwargs: dict = {"api_key": config.api_key}
        if config.base_url:
            client_kwargs["http_options"] = types.HttpOptions(base_url=config.base_url)
        self._client: genai.Client = genai.Client(**client_kwargs)
        self._session: AsyncSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._io_thread: threading.Thread | None = None
        self._resumption_handle: str | None = None
        self._speech_ended_at: float | None = None
        self._first_audio_received: bool = False
        self._vad_disabled: bool = not config.vad_enabled
        self._activity_started: bool = False
        self._reconnect_delay_s: float = self.DEFAULT_RECONNECT_DELAY_S
        self._send_timeout_s: float = self.DEFAULT_SEND_TIMEOUT_S
        self._recv_timeout_s: float = self.DEFAULT_RECV_TIMEOUT_S
        self._queue_poll_s: float = self.DEFAULT_QUEUE_POLL_S
        self._join_timeout_s: float = self.DEFAULT_JOIN_TIMEOUT_S
        # Signals that the model is idle (no active turn). Set by default,
        # cleared when activityEnd is sent, set again on turn_complete.
        self._turn_done: threading.Event = threading.Event()
        self._turn_done.set()

    @property
    @override
    def sample_rate(self) -> int:
        return self._config.sample_rate

    def _build_config(self) -> types.LiveConnectConfig:
        lang: str | None = self._config.language
        lang_codes: list[str] | None = (
            [lang] if lang and self._config.use_language_codes else None
        )

        live_config: types.LiveConnectConfig = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._config.voice.value,
                    )
                ),
                language_code=lang,
            ),
            system_instruction=self._config.instructions,
            input_audio_transcription=None,
            output_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=lang_codes,
            ),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=self._vad_disabled,
                ),
            ),
            thinking_config=types.ThinkingConfig(
                thinking_level=self._config.thinking_level.value,
            ),
            context_window_compression=(
                types.ContextWindowCompressionConfig(
                    sliding_window=types.SlidingWindow(),
                )
                if self._config.context_window_compression
                else None
            ),
        )

        if self._tools:
            declarations: list[types.FunctionDeclaration] = [
                types.FunctionDeclaration(
                    name=tool["name"],
                    description=tool.get("description", ""),
                    parameters=tool.get("parameters"),
                )
                for tool in self._tools
            ]
            live_config.tools = [types.Tool(function_declarations=declarations)]

        if self._resumption_handle is not None:
            live_config.session_resumption = types.SessionResumptionConfig(
                handle=self._resumption_handle,
            )

        return live_config

    # --- Async internals (run on private event loop) ---

    async def _async_connect(self) -> None:
        logger.info(
            "Connecting to Gemini Live API (base_url=%s, model=%s)",
            self._config.base_url,
            self._config.model,
        )
        self._exit_stack = AsyncExitStack()
        self._session = await self._exit_stack.enter_async_context(
            self._client.aio.live.connect(
                model=self._config.model,
                config=self._build_config(),
            )
        )
        logger.info("Gemini Live session open (voice=%s)", self._config.voice)

    async def _async_disconnect(self) -> None:
        if self._exit_stack is not None:
            logger.info("Disconnecting from Gemini Live API")
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None

    async def _async_send_input(self, input: InputBase) -> None:
        if self._session is None:
            return
        if isinstance(input, AudioInput):
            # When VAD is disabled, send activityStart before first audio
            if self._vad_disabled and not self._activity_started:
                await self._session.send_realtime_input(
                    activity_start=types.ActivityStart()
                )
                self._activity_started = True
                logger.debug("Sent activityStart (manual VAD)")

            self._speech_ended_at = time.perf_counter()
            pcm_bytes: bytes = float32_to_pcm16_bytes(input.audio)
            await self._session.send_realtime_input(
                audio=types.Blob(
                    data=pcm_bytes,
                    mime_type=f"audio/pcm;rate={self._config.sample_rate}",
                )
            )
        elif isinstance(input, TextInput):
            await self._session.send_client_content(
                turns=types.Content(
                    parts=[types.Part(text=input.text)],
                    role="user",
                ),
                turn_complete=False,
            )
        elif isinstance(input, ImageInput):
            _: bool
            buf: npt.NDArray[np.uint8]
            _, buf = cv2.imencode(".jpg", input.image)
            await self._session.send_realtime_input(
                video=types.Blob(data=buf.tobytes(), mime_type="image/jpeg")
            )
        elif isinstance(input, FunctionCallResultInput):
            await self._session.send_tool_response(
                function_responses=[
                    types.FunctionResponse(
                        id=input.call_id,
                        response=json.loads(input.output),
                    )
                ]
            )

    async def _async_commit(self) -> None:
        if self._session is None:
            return
        if self._vad_disabled and self._activity_started:
            await self._session.send_realtime_input(activity_end=types.ActivityEnd())
            self._activity_started = False
            self._turn_done.clear()
            logger.debug("Sent activityEnd (manual VAD)")

    async def _async_receive_turn(self) -> None:
        """Read one full turn from the session, put outputs on _recv_queue."""
        if self._session is None:
            return
        self._first_audio_received = False

        async for message in self._session.receive():
            if message.server_content:
                content = message.server_content

                if content.model_turn and content.model_turn.parts:
                    for part in content.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            if not self._first_audio_received:
                                self._first_audio_received = True
                                if self._speech_ended_at is not None:
                                    latency_ms: float = (
                                        time.perf_counter() - self._speech_ended_at
                                    ) * 1000
                                    logger.info("Response latency: %.0fms", latency_ms)
                                    self._speech_ended_at = None
                            self._recv_queue.put(
                                OutputEvent(
                                    output=AudioOutput(
                                        audio=pcm16_bytes_to_float32(
                                            part.inline_data.data
                                        )
                                    ),
                                )
                            )
                        elif part.text:
                            self._recv_queue.put(
                                OutputEvent(output=TextOutput(text=part.text))
                            )

                if content.output_transcription and content.output_transcription.text:
                    self._recv_queue.put(
                        OutputEvent(
                            output=TextOutput(text=content.output_transcription.text),
                        )
                    )

                if content.interrupted:
                    logger.debug("Response interrupted")
                    self._first_audio_received = False
                    self._turn_done.set()

                if content.turn_complete:
                    logger.debug("Turn complete")
                    self._first_audio_received = False
                    self._turn_done.set()
                    self._recv_queue.put(TurnDoneEvent())
                    return

            elif message.tool_call and message.tool_call.function_calls:
                for fc in message.tool_call.function_calls:
                    logger.debug("Function call: %s (call_id=%s)", fc.name, fc.id)
                    self._recv_queue.put(
                        OutputEvent(
                            output=FunctionCallOutput(
                                name=fc.name or "",
                                arguments=json.dumps(fc.args) if fc.args else "{}",
                                call_id=fc.id or "",
                            ),
                        )
                    )

            if message.session_resumption_update:
                update = message.session_resumption_update
                if update.new_handle:
                    self._resumption_handle = update.new_handle

            if message.go_away:
                logger.warning(
                    "Server go_away (time_left=%s)", message.go_away.time_left
                )
                raise ConnectionClosed(None, None)

    # --- Reconnect ---

    def _reconnect(self) -> None:
        self._connected.clear()
        self._activity_started = False
        self._turn_done.set()  # unblock any waiting commit
        if self._loop is None:
            logger.error("Cannot reconnect — event loop is None")
            return
        try:
            logger.info("Reconnecting...")
            self._submit_and_wait(self._async_disconnect())
            self._submit_and_wait(self._async_connect())
            self._connected.set()
        except Exception as e:
            logger.warning("Reconnect failed: %s — will retry after delay", e)
            time.sleep(self._reconnect_delay_s)

    # --- VoiceAgentBase implementation ---

    def _submit_and_wait(self, coro: Any, timeout: float = 30.0) -> Any:
        """Submit a coroutine to the IO thread's loop and block until done."""
        if self._loop is None:
            raise RuntimeError("Event loop is None")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(
            timeout=timeout
        )

    @override
    def _do_connect(self) -> None:
        """Spawn IO thread with event loop, connect on it. Blocks until ready."""
        self._loop = asyncio.new_event_loop()
        self._io_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="gemini-io",
        )
        self._io_thread.start()
        self._submit_and_wait(self._async_connect())

    @override
    def _do_disconnect(self) -> None:
        if self._loop is not None:
            try:
                self._submit_and_wait(self._async_disconnect())
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._io_thread is not None:
                self._io_thread.join(timeout=self._join_timeout_s)
                self._io_thread = None
            self._loop.close()
            self._loop = None

    @override
    def _send_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._loop is None:
                break
            try:
                event: AgentInputEvent = self._send_queue.get(
                    timeout=self._queue_poll_s
                )
            except queue.Empty:
                continue

            try:
                if isinstance(event, AudioCommitEvent):
                    if not self._turn_done.wait(timeout=10.0):
                        logger.warning("Timed out waiting for turn to finish — forcing commit")
                    self._submit_and_wait(
                        self._async_commit(), timeout=self._send_timeout_s
                    )
                elif isinstance(event, InputEvent):
                    self._submit_and_wait(
                        self._async_send_input(event.input),
                        timeout=self._send_timeout_s,
                    )
            except (ConnectionClosed, genai_errors.APIError) as e:
                logger.warning("Send failed: %s — reconnecting", e)
                self._reconnect()
            except Exception as e:
                logger.warning("Send error: %s — reconnecting", e)
                self._reconnect()

    @override
    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._loop is None:
                break
            if not self._connected.is_set():
                _ = self._connected.wait(timeout=self._queue_poll_s)
                continue
            try:
                self._submit_and_wait(
                    self._async_receive_turn(), timeout=self._recv_timeout_s
                )
            except (ConnectionClosed, genai_errors.APIError) as e:
                logger.warning("Recv failed: %s — reconnecting", e)
                self._reconnect()
            except Exception as e:
                logger.exception("Unexpected error in recv loop: %s", e)
                self._reconnect()
