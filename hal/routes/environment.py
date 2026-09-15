"""Read-only HAL environmental sensor endpoints."""

from fastapi import APIRouter, HTTPException

import hal.app_state as state

router = APIRouter(prefix="/environment", tags=["Environment"])


@router.get("/status")
def environment_status():
    if state.environment_service is None:
        raise HTTPException(503, "Environment service not initialized")
    return state.environment_service.snapshot()


@router.get("/sample")
def environment_sample():
    snapshot = environment_status()
    if snapshot["stale"]:
        raise HTTPException(503, "No fresh environmental sample available")
    return snapshot
