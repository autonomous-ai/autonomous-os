"""HTTP endpoints for the Speech Emotion Recognition (SER) service."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from dlserver.models.audio_emotion import (
    LabelsResponse,
    RecognizeEmotionRequest,
    RecognizeEmotionResponse,
)
from dlserver.utils.audio import decode_b64_wav
from dlserver.utils.state import get_audio_emotion_model

logger: logging.Logger = logging.getLogger(__name__)

router = APIRouter(tags=["ser"])


@router.post("/ser/recognize", response_model=RecognizeEmotionResponse)
async def recognize_emotion(req: RecognizeEmotionRequest):
    """Classify the emotion of a single utterance from base64-encoded WAV."""
    model = get_audio_emotion_model()
    if model is None or not model.is_ready():
        raise HTTPException(status_code=503, detail="Audio emotion model not loaded")

    try:
        audio = decode_b64_wav(req.audio_b64)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid audio format")

    try:
        detection = await model.predict_audio(audio)
        return RecognizeEmotionResponse.from_detection(detection, return_scores=req.return_scores)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error processing audio emotion HTTP message")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/ser/labels", response_model=LabelsResponse)
async def list_emotion_labels():
    """Return the active engine name + ordered label list."""
    model = get_audio_emotion_model()
    if model is None or not model.is_ready():
        raise HTTPException(status_code=503, detail="Audio emotion model not loaded")
    return LabelsResponse(engine=model.engine_name, labels=list(model.labels))
