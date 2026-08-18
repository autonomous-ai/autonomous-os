"""Publish the captured `look` frame to the Flow Monitor.

The realtime `look` tool already saves its frame (see
`RealtimeOrchestrator._persist_look_frame`) so a delegate turn can hand it to
the main agent by path — but nothing announced it, so the frame never reached
the monitor. Operators debugging an aim had to SSH to the device to see what
the camera actually captured, which the orchestrator's own comment asks for:
"pull the file and compare it against the model's answer".

Nothing else is needed to render it. The frame already lives under the agent
workspace (`/root/.<runtime>/media/hal-snapshots/look_<ms>.jpg`), which the
monitor frontend already recognises and serves via
`/api/sensing/agent-snapshot/...`. So this only has to get the path into a flow
event; the existing URL builder and endpoint do the rest.

Unlike a `motion.activity` snapshot — surfaced in the UI but stripped before
the LLM — this frame IS what was sent to the model. The thumbnail is the
literal input, not a debug approximation of it.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

import hal.config as config

logger = logging.getLogger(__name__)

# Monitor-only sensing type. The os-server handler logs the flow event and
# stops: forwarding text to the agent would inject a phantom turn, because the
# frame already went straight to the realtime model.
LOOK_CAPTURE_EVENT: str = "look.capture"

_POST_TIMEOUT_S: float = 1.0


def publish_look_frame(path: Optional[str]) -> bool:
    """Announce a captured look frame. Best-effort: never raises, never blocks
    the turn for long — a missing thumbnail must not cost the user an answer."""
    if not path:
        return False
    try:
        resp = requests.post(
            config.OS_SENSING_URL,
            json={
                "type": LOOK_CAPTURE_EVENT,
                # The raw path is what the monitor's URL builder matches on.
                # It never reaches the agent: the handler drops this type after
                # logging, and the browser only ever sees the derived
                # /api/sensing/agent-snapshot/ URL, not the device path.
                "message": f"realtime look captured {path}",
            },
            timeout=_POST_TIMEOUT_S,
        )
        if resp.status_code >= 400:
            logger.debug("[look-monitor] publish returned %s", resp.status_code)
            return False
        return True
    except Exception as e:
        logger.debug("[look-monitor] publish failed: %s", e)
        return False
