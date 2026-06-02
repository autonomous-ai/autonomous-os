"""DL Backend — FastAPI server.

Thin shell: app creation, lifespan (model loading), router registration.

Usage:
    python -m dlserver                  # default 0.0.0.0:8001
    python -m dlserver --port 9000      # custom port
    python -m dlserver --host 127.0.0.1 # localhost only
"""

import argparse
import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader

from config import settings
from dlserver.routes.action import router as action_ws_router
from dlserver.routes.audio import router as audio_router
from dlserver.routes.audio_emotion import router as audio_emotion_router
from dlserver.routes.facial_emotion import http_router as emotion_http_router
from dlserver.routes.facial_emotion import ws_router as emotion_ws_router
from dlserver.routes.health import router as health_router
from dlserver.routes.object import http_router as object_http_router
from dlserver.routes.object import ws_router as object_ws_router
from dlserver.routes.pose import ws_router as pose_ws_router
from dlserver.utils.state import (
    get_action_model,
    get_audio_embedder,
    get_audio_emotion_model,
    get_emotion_model,
    get_object_models,
    get_pose_model,
    set_action_model,
    set_audio_embedder,
    set_audio_emotion_model,
    set_emotion_model,
    set_object_models,
    set_pose_model,
)
from factory import (
    build_action_perception,
    build_audio_embedder,
    build_audio_emotion_perception,
    build_emotion_perception,
    build_object_perceptions,
    build_pose_perception,
)

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
logger = logging.getLogger(__name__)

# --- Auth ---

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)):
    """Validate the X-API-Key header against DL_API_KEY."""
    if not settings.dl_api_key:
        return
    if not api_key or not secrets.compare_digest(api_key, settings.dl_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# --- Lifespan ---


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load models at startup, release on shutdown."""

    # -- Action model --
    if settings.action.enabled:
        logger.info("Loading action model...")
        try:
            action_model = build_action_perception()
            await action_model.start()
            set_action_model(action_model)
            logger.info("Action model ready")
        except Exception as e:
            logger.error("Failed to load action model: %s", e)

    # -- Emotion model --
    if settings.fer.enabled:
        logger.info("Loading emotion model...")
        try:
            emotion_model = build_emotion_perception()
            await emotion_model.start()
            set_emotion_model(emotion_model)
            logger.info("Emotion model ready")
        except Exception as e:
            logger.error("Failed to load emotion model: %s", e)

    # -- Audio embedder --
    if settings.audio_embedder.enabled:
        logger.info("Loading audio embedder...")
        try:
            audio_embedder = build_audio_embedder()
            await asyncio.to_thread(audio_embedder.start)
            set_audio_embedder(audio_embedder)
            logger.info("Audio embedder ready")
        except Exception as e:
            logger.error("Failed to load audio embedder: %s", e)

    # -- Audio emotion model --
    if settings.ser.enabled:
        logger.info("Loading audio emotion model...")
        try:
            audio_emotion_model = build_audio_emotion_perception()
            await audio_emotion_model.start()
            set_audio_emotion_model(audio_emotion_model)
            logger.info("Audio emotion model ready")
        except Exception as e:
            logger.error("Failed to load audio emotion model: %s", e)

    # -- Pose estimator --
    if settings.pose.enabled:
        logger.info("Loading pose estimator...")
        try:
            pose_model = build_pose_perception()
            await pose_model.start()
            set_pose_model(pose_model)
            logger.info("Pose estimator ready")
        except Exception as e:
            logger.error("Failed to load pose estimator: %s", e)

    # -- Object detectors --
    logger.info("Loading object detectors...")
    object_models = build_object_perceptions()
    for name, model in list(object_models.items()):
        try:
            await model.start()
            logger.info("Object detector '%s' ready", name)
        except Exception as e:
            logger.error("Failed to load object detector '%s': %s", name, e)
            del object_models[name]
    set_object_models(object_models)

    yield

    logger.info("Shutting down DL backend...")
    action_model = get_action_model()
    if action_model is not None:
        await action_model.stop()
    emotion_model = get_emotion_model()
    if emotion_model is not None:
        await emotion_model.stop()
    pose_model = get_pose_model()
    if pose_model is not None:
        await pose_model.stop()
    for name, model in get_object_models().items():
        await model.stop()
    audio_embedder = get_audio_embedder()
    if audio_embedder is not None:
        await asyncio.to_thread(audio_embedder.stop)
    audio_emotion_model = get_audio_emotion_model()
    if audio_emotion_model is not None:
        await audio_emotion_model.stop()
    logger.info("DL backend shutdown complete")


# --- App + Routers ---

app = FastAPI(title="DL Backend", lifespan=lifespan)

# Existing perceptions — /lelamp/api/dl/ prefix
app.include_router(action_ws_router, prefix="/lelamp/api/dl")
app.include_router(emotion_ws_router, prefix="/lelamp/api/dl")
app.include_router(emotion_http_router, prefix="/lelamp/api/dl", dependencies=[Depends(verify_api_key)])
app.include_router(health_router, prefix="/lelamp/api/dl", dependencies=[Depends(verify_api_key)])
app.include_router(
    audio_router, prefix="/lelamp/api/dl", dependencies=[Depends(verify_api_key)]
)
app.include_router(audio_emotion_router, prefix="/lelamp/api/dl", dependencies=[Depends(verify_api_key)])
app.include_router(pose_ws_router, prefix="/lelamp/api/dl")


# Object detection — /api/dl/ prefix (backward-compatible with go2)
app.include_router(object_ws_router, prefix="/api/dl")
app.include_router(object_http_router, prefix="/api/dl", dependencies=[Depends(verify_api_key)])


# --- CLI ---


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DL Backend Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8001, help="Bind port (default: 8001)")
    parser.add_argument("--log-dir", default=None, help="Directory for rotating log files")
    parser.add_argument("--pid-file", default=None, help="Write PID to this file")
    return parser.parse_args()


def _setup_logging(log_dir: str | None) -> None:
    if log_dir:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        Path(log_dir).mkdir(parents=True, exist_ok=True)
        # Clean up old .bak files, then rename current logs to .bak
        for bak in Path(log_dir).glob("dlserver.log*.bak"):
            bak.unlink()
        for old in Path(log_dir).glob("dlserver.log*"):
            old.rename(Path(str(old) + ".bak"))
        log_path = Path(log_dir) / "dlserver.log"
        handler = RotatingFileHandler(
            str(log_path), maxBytes=1_048_576, backupCount=3
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    else:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def main() -> None:
    args = parse_args()
    _setup_logging(args.log_dir)

    if args.pid_file:
        from pathlib import Path

        Path(args.pid_file).write_text(str(os.getpid()))

    logger.info("Starting DL backend on %s:%d (pid=%d)", args.host, args.port, os.getpid())
    uvicorn.run(app, host=args.host, port=args.port)
