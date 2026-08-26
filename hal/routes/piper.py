"""Piper install + voice download, driven from the admin UI.

Piper is not part of any OTA component, so a device that shipped before it
existed has no /opt/piper. Rather than grow the image or the HAL package by
~90 MB for every unit — including the ones that will never switch off the
hosted voice — the operator asks for it and the device fetches it then.

Two steps, in order: install the engine, then download a voice. Both run on a
background thread and report progress, because a 63 MB pull over a domestic
connection is far longer than an HTTP request should be held open for.
"""

import logging
import os
import shutil
import tarfile
import tempfile
import threading
import urllib.request
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from hal.drivers.voice.tts.piper_catalog import (
    BINARY_URL, CATALOG, DEFAULT_VOICE, voice_urls,
)

router = APIRouter()
logger = logging.getLogger("hal.voice.piper")

PIPER_DIR = os.environ.get("HAL_PIPER_DIR", "/opt/piper")
VOICES_DIR = os.environ.get("HAL_PIPER_VOICES", os.path.join(PIPER_DIR, "voices"))
BIN_PATH = os.path.join(PIPER_DIR, "piper")

# Single in-flight job. Two concurrent 63 MB downloads onto a device with one
# slow card and a RAM-backed /tmp is not a case worth supporting; the UI
# disables its buttons while a job runs and this is the server-side backstop.
_job_lock = threading.Lock()
_job = {"active": False, "kind": "", "target": "", "percent": 0, "error": "", "done": False}


def _set(**kw):
    with _job_lock:
        _job.update(kw)


def _snapshot() -> dict:
    with _job_lock:
        return dict(_job)


def _download(url: str, dest: str, weight_from: int, weight_to: int) -> None:
    """Fetch url to dest, mapping its progress onto a slice of the overall job.

    Writes to a .part file and renames on success so an interrupted download
    can never leave a truncated .onnx that Piper would later fail to load in a
    way that looks like a corrupt install.
    """
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if total:
                span = weight_to - weight_from
                _set(percent=weight_from + int(span * got / total))
    os.replace(tmp, dest)


def _install_binary_job() -> None:
    try:
        _set(active=True, kind="engine", target="piper", percent=0, error="", done=False)
        os.makedirs(PIPER_DIR, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            tar_path = os.path.join(td, "piper.tar.gz")
            _download(BINARY_URL, tar_path, 0, 85)
            _set(percent=88)
            with tarfile.open(tar_path) as tf:
                tf.extractall(td)          # noqa: S202 — trusted release tarball
            src = os.path.join(td, "piper")
            for entry in os.listdir(src):
                s, d = os.path.join(src, entry), os.path.join(PIPER_DIR, entry)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
        os.chmod(BIN_PATH, 0o755)
        os.makedirs(VOICES_DIR, exist_ok=True)
        _set(percent=100, done=True, active=False)
        logger.info("Piper engine installed at %s", PIPER_DIR)
    except Exception as e:
        logger.warning("Piper engine install failed: %s", e)
        _set(error=str(e), active=False, done=True)


def _install_voice_job(name: str) -> None:
    try:
        _set(active=True, kind="voice", target=name, percent=0, error="", done=False)
        urls = voice_urls(name)
        if not urls:
            raise ValueError(f"voice {name!r} is not in the catalogue")
        os.makedirs(VOICES_DIR, exist_ok=True)
        onnx_url, json_url = urls
        # The .json sidecar is tiny but carries the sample rate, so fetch it
        # first: a model without it would load at the wrong rate and sound
        # pitched, which is far more confusing than a failed download.
        _download(json_url, os.path.join(VOICES_DIR, f"{name}.onnx.json"), 0, 3)
        _download(onnx_url, os.path.join(VOICES_DIR, f"{name}.onnx"), 3, 100)
        _set(percent=100, done=True, active=False)
        logger.info("Piper voice %s downloaded", name)
    except Exception as e:
        logger.warning("Piper voice %s download failed: %s", name, e)
        _set(error=str(e), active=False, done=True)


def _installed_voices() -> list:
    try:
        return sorted(
            f[: -len(".onnx")] for f in os.listdir(VOICES_DIR) if f.endswith(".onnx")
        )
    except OSError:
        return []


@router.get("/voice/piper/status")
def piper_status():
    """What is installed, what can be installed, and any job in flight."""
    installed = _installed_voices()
    return {
        "engine_installed": os.path.isfile(BIN_PATH) and os.access(BIN_PATH, os.X_OK),
        "voices_installed": installed,
        "default_voice": DEFAULT_VOICE,
        "catalog": [
            {
                "name": name,
                "installed": name in installed,
                **{k: v for k, v in meta.items() if k != "path"},
            }
            for name, meta in CATALOG.items()
        ],
        "job": _snapshot(),
    }


class VoiceRequest(BaseModel):
    name: Optional[str] = None


@router.post("/voice/piper/install")
def piper_install():
    """Install the engine. Idempotent: already-installed is a success, not an
    error, so the UI can call it without first checking."""
    if _snapshot()["active"]:
        return {"status": "busy", "job": _snapshot()}
    if os.path.isfile(BIN_PATH):
        return {"status": "ok", "already": True}
    threading.Thread(target=_install_binary_job, daemon=True).start()
    return {"status": "started"}


@router.post("/voice/piper/voice")
def piper_voice(req: VoiceRequest):
    """Download one catalogue voice."""
    name = (req.name or DEFAULT_VOICE).strip()
    if name not in CATALOG:
        return {"status": "error", "message": f"unknown voice {name!r}"}
    if _snapshot()["active"]:
        return {"status": "busy", "job": _snapshot()}
    if name in _installed_voices():
        return {"status": "ok", "already": True}
    threading.Thread(target=_install_voice_job, args=(name,), daemon=True).start()
    return {"status": "started"}
