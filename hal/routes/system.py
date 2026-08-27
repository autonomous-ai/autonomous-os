"""System route handlers -- /system/reboot, /system/shutdown."""

import threading

from fastapi import APIRouter

from hal.drivers.button_actions import reboot_action, shutdown_action
from hal.models import StatusResponse

router = APIRouter(tags=["System"])


@router.post("/system/reboot", response_model=StatusResponse)
def reboot_os():
    """Run the full reboot action after acknowledging the HTTP caller."""
    threading.Thread(
        target=reboot_action,
        kwargs={"source": "system API"},
        daemon=True,
        name="system-api-reboot",
    ).start()
    return {"status": "rebooting"}


@router.post("/system/shutdown", response_model=StatusResponse)
def shutdown_os():
    """Run the full shutdown action after acknowledging the HTTP caller."""
    threading.Thread(
        target=shutdown_action,
        kwargs={"source": "system API"},
        daemon=True,
        name="system-api-shutdown",
    ).start()
    return {"status": "shutting down"}
