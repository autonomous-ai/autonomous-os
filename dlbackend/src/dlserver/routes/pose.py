"""Pose estimation WebSocket + HTTP endpoints."""

import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from dlserver.models.pose import (
    PoseConfigRequest,
    PoseEstimateRequest,
    PoseEstimateResponse,
    PoseFrameRequest,
    PoseHeartBeatRequest,
    PoseRequest,
    PoseResponse,
)
from dlserver.utils.common import decode_image, verify_ws_api_key
from dlserver.utils.state import get_pose_model

logger: logging.Logger = logging.getLogger(__name__)

ws_router: APIRouter = APIRouter()
http_router: APIRouter = APIRouter()
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

    try:
        session = await pose_model.create_session()
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
                await websocket.send_json({"error": str(e)})

    except WebSocketDisconnect:
        logger.info("Pose estimation WebSocket disconnected")


@http_router.post("/pose-estimate", response_model=PoseEstimateResponse)
async def pose_estimate(req: PoseEstimateRequest):
    """Single-shot pose estimation from a base64-encoded image.

    Returns 2D keypoints (always) and 3D joints (if 3D lifter is configured).
    """
    pose_model = get_pose_model()
    if pose_model is None or not pose_model.is_ready():
        raise HTTPException(status_code=503, detail="Pose model not loaded")

    frame = decode_image(req.image_b64)
    session = await pose_model.create_session()
    result = await session.update(frame)
    if result is None:
        raise HTTPException(status_code=500, detail="Pose estimation failed")

    logger.info("[Pose] Estimated %d joints", len(result.pose_2d.joints))
    return PoseEstimateResponse.from_pose_detection(result)
