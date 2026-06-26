"""Pose estimation WebSocket endpoint."""

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from dlserver.models.pose import (
    PoseConfigRequest,
    PoseFrameRequest,
    PoseHeartBeatRequest,
    PoseRequest,
    PoseResponse,
)
from dlserver.utils.common import decode_image, verify_ws_api_key
from dlserver.utils.state import get_pose_model

logger: logging.Logger = logging.getLogger(__name__)

ws_router: APIRouter = APIRouter()
_request_adapter: TypeAdapter[PoseRequest] = TypeAdapter(PoseRequest)


@ws_router.websocket("/pose-estimation/ws")
async def pose_estimation_ws(websocket: WebSocket):
    """WebSocket endpoint for streaming pose estimation.

    Accepts JSON messages with a "type" field:
    - {"type": "frame", "task": "pose", "frame_b64": "<base64>"} — feed a frame
    - {"type": "config", "task": "pose", "frame_interval": 0.1} — update config
    - {"type": "heartbeat", "task": "pose"} — keep-alive

    API key is validated from the X-API-Key header on connect.
    """
    if not await verify_ws_api_key(websocket):
        return

    await websocket.accept()

    pose_model = get_pose_model()
    if pose_model is None or not pose_model.is_ready():
        await websocket.close(code=1011, reason="Pose model not loaded")
        return

    session = await pose_model.create_session()
    try:
        while True:
            raw: str = await websocket.receive_text()
            try:
                req = _request_adapter.validate_json(raw)
            except ValidationError as e:
                await websocket.send_json({"error": e.errors()})
                continue

            try:
                match req:
                    case PoseFrameRequest():
                        frame = decode_image(req.frame_b64)
                        result = await session.update(frame)
                        if result is not None:
                            response = PoseResponse.from_pose_detection(result)
                            await websocket.send_json(response.model_dump(exclude_none=True))

                    case PoseConfigRequest():
                        session.update_config(
                            frame_interval=req.frame_interval,
                            confidence_threshold_2d=req.confidence_threshold_2d,
                            min_valid_keypoints=req.min_valid_keypoints,
                        )
                        await websocket.send_json({"status": "config_updated"})

                    case PoseHeartBeatRequest():
                        await websocket.send_json({"status": "ok"})

                    case _:
                        logger.warning("Unknown pose WS message type: %s", raw[:200])
            except WebSocketDisconnect:
                raise
            except Exception as e:
                logger.exception("Error processing pose WS message")
                await websocket.send_json({"error": "Processing failed"})

    except WebSocketDisconnect:
        logger.info("Pose estimation WebSocket disconnected")
    except Exception:
        logger.exception("Pose estimation WebSocket handler crashed")
    finally:
        await session.stop()
