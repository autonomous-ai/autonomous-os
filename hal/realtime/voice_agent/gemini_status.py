"""Preserve Gemini interaction status dropped by older google-genai converters."""

from contextvars import ContextVar
import json


class _StatusWebSocket:
    """Forward the socket unchanged while observing each receive's own payload."""

    def __init__(self, socket, current_receive):
        self._socket = socket
        self._current_receive = current_receive

    def __getattr__(self, name):
        return getattr(self._socket, name)

    async def recv(self, *args, **kwargs):
        raw = await self._socket.recv(*args, **kwargs)
        capture = self._current_receive.get()
        if capture is not None:
            # Keep parsing and transport errors under the SDK's ownership.
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError, UnicodeError):
                payload = None
            content = payload.get("serverContent") if isinstance(payload, dict) else None
            status = content.get("interactionStatus") if isinstance(content, dict) else None
            capture["status"] = status if isinstance(status, str) else None
        return raw


def install_interaction_status(session):
    """Attach raw serverContent.interactionStatus to this session's parsed messages.

    google-genai 2.12.1 strips this field in its converter. Wrap only this live
    session, retaining the SDK's parsing, validation, and exception behavior.
    The context-local capture associates metadata with the exact receive call;
    missing fields never inherit status from a preceding message.
    """
    if isinstance(session._ws, _StatusWebSocket):
        return
    current_receive = ContextVar("gemini_interaction_status", default=None)
    original_receive = session._receive
    session._ws = _StatusWebSocket(session._ws, current_receive)

    async def receive_with_status():
        capture = {}
        token = current_receive.set(capture)
        try:
            message = await original_receive()
            status = capture.get("status")
            if status is not None and message.server_content is not None:
                content = message.server_content.model_copy(
                    update={"interaction_status": status},
                )
                message = message.model_copy(update={"server_content": content})
            return message
        finally:
            current_receive.reset(token)

    session._receive = receive_with_status
